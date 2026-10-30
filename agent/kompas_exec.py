"""
Исполнитель примитивов DSL в КОМПАС-3D через COM (API5 + API7), только Windows.

ВАЖНО: этот модуль писался без доступа к КОМПАСу. Перед первым запуском:
  1) python kompas_selftest.py        — калибровка осей плоскостей и единиц МЦХ,
                                         затем сверка каждой операции с refgeom;
  2) если какая-то операция падает — запишите её макросом в КОМПАСе
     (Приложения → Python → запись макроса, если есть в вашей версии) или найдите
     пример в SDK и поправьте соответствующий метод ниже. Методы короткие, по одному на операцию.

Калибровка (kompas_calibration.json) хранит для каждой плоскости:
  su, sv — знаки осей эскиза (u, v) относительно глобальных осей,
  sn     — совпадает ли «прямое направление» выдавливания с +w,
  so     — совпадает ли direction=True смещённой плоскости с +w,
  swap   — оси эскиза переставлены (u ↔ v), так ведёт себя плоскость YOZ в КОМПАСе;
  sc     — дополнительный знак направления для ВЫРЕЗА (в КОМПАСе вырез идёт
           противоположно выдавливанию при том же directionType).
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

DEFAULT_CALIB = {
    "planes": {p: {"su": 1, "sv": 1, "sn": 1, "so": 1, "sc": -1, "swap": 0} for p in ("XY", "XZ", "YZ")},
    "volume_scale": 1.0,
}
PLANE_ENTITY = {"XY": "o3d_planeXOY", "XZ": "o3d_planeXOZ", "YZ": "o3d_planeYOZ"}


class BuildError(RuntimeError):
    pass


def _mirror_u(c: dict) -> dict:
    m = lambda q: (-q[0], q[1])  # noqa: E731
    if c["kind"] == "circle":
        return {"kind": "circle", "c": m(c["c"]), "r": c["r"]}
    segs = []
    for sg in c["segs"]:
        if sg[0] == "L":
            segs.append(("L", m(sg[1]), m(sg[2])))
        else:  # отражение меняет направление обхода дуги
            segs.append(("A", m(sg[1]), sg[2], m(sg[3]), m(sg[4]), not sg[5]))
    return {"kind": "loop", "segs": segs}


def _shift_contour(c: dict, du: float, dv: float) -> dict:
    sh = lambda q: (q[0] + du, q[1] + dv)  # noqa: E731
    if c["kind"] == "circle":
        return {"kind": "circle", "c": sh(c["c"]), "r": c["r"]}
    segs = []
    for sg in c["segs"]:
        if sg[0] == "L":
            segs.append(("L", sh(sg[1]), sh(sg[2])))
        else:
            segs.append(("A", sh(sg[1]), sg[2], sh(sg[3]), sh(sg[4]), sg[5]))
    return {"kind": "loop", "segs": segs}


def load_calibration(path: str | os.PathLike = "kompas_calibration.json") -> dict:
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(json.dumps(DEFAULT_CALIB))


class Kompas:
    def __init__(self, calib: dict | None = None, visible: bool = True):
        import pythoncom  # noqa: F401  (pywin32)
        from win32com.client import Dispatch, gencache

        self.calib = calib or load_calibration()
        self.c5 = gencache.EnsureModule("{75C9F5D0-B5B8-4526-8681-9903C567D2ED}", 0, 1, 0).constants
        self.c3 = gencache.EnsureModule("{2CAF168C-7961-4B90-9DA2-701419BEEFE3}", 0, 1, 0).constants
        api5 = gencache.EnsureModule("{0422828C-F174-495E-AC5D-D31014DBBE87}", 0, 1, 0)
        api7 = gencache.EnsureModule("{69AC2981-37C0-4379-84FD-5DD2F3C0A520}", 0, 1, 0)
        self.api7 = api7
        import pythoncom as pc
        self.k5 = api5.KompasObject(
            Dispatch("Kompas.Application.5")._oleobj_.QueryInterface(api5.KompasObject.CLSID, pc.IID_IDispatch))
        self.app7 = api7.IApplication(
            Dispatch("Kompas.Application.7")._oleobj_.QueryInterface(api7.IApplication.CLSID, pc.IID_IDispatch))
        self.app7.Visible = visible
        try:  # не показывать модальные окна с вопросами
            self.app7.HideMessage = getattr(self.c5, "ksHideMessageNo", 2)
        except Exception:
            pass

    # ------------------------------------------------------------ документ
    def new_part(self):
        self.doc = self.k5.Document3D()
        if not self.doc.Create(False, True):
            raise BuildError("не удалось создать документ детали")
        self.part = self.doc.GetPart(self.c3.pTop_Part)
        self._planes: dict = {}
        self._has_body = False

    def close(self):
        try:
            self.doc.close()
        except Exception:
            pass

    def save(self, path: str):
        import time
        p = Path(path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        if self.doc.SaveAs(str(p)):
            return str(p)
        # файл занят (открыт в КОМПАСе?) — пробуем удалить старый
        try:
            if p.exists():
                p.unlink()
                if self.doc.SaveAs(str(p)):
                    return str(p)
        except OSError:
            pass
        alt = p.with_name(f"{p.stem}_{time.strftime('%H%M%S')}{p.suffix}")
        if self.doc.SaveAs(str(alt)):
            print(f"ВНИМАНИЕ: {p.name} занят (открыт в КОМПАСе?), сохранено как {alt.name}")
            return str(alt)
        raise BuildError(f"не удалось сохранить {p} — закройте этот файл в КОМПАСе")

    def verify_reopen(self, path: str) -> bool:
        d = self.k5.Document3D()
        ok = bool(d.Open(str(Path(path).resolve()), False))
        try:
            d.close()
        except Exception:
            pass
        return ok

    # ------------------------------------------------------------ плоскости и эскизы
    def _cal(self, plane, rot: bool = False):
        P = self.calib["planes"]
        if rot:  # плоскость под углом к XZ/YZ — своя калибровка (ключ "XZr"/"YZr")
            return P.get(plane + "r") or {**P[plane], "sa": 1}
        return P[plane]

    def _plane(self, plane: str, offset: float, angle: float | None = None):
        key = (plane, round(offset, 9), None if angle is None else round(angle, 9))
        if key in self._planes:
            return self._planes[key]
        base = self.part.GetDefaultEntity(getattr(self.c3, PLANE_ENTITY[plane]))
        cal = self._cal(plane, angle is not None)
        if angle is not None:  # плоскость под углом вокруг оси Z
            ap = self.part.NewEntity(self.c3.o3d_planeAngle)
            d = ap.GetDefinition()
            d.SetPlane(base)
            d.SetAxis(self.part.GetDefaultEntity(self.c3.o3d_axisOZ))
            d.angle = float(angle)  # уже в соглашении КОМПАСа, 0…180°
            ap.hidden = True
            if not ap.Create():
                raise BuildError(f"не удалось создать плоскость под углом {angle}° к {plane}")
            base = ap
        if abs(offset) < 1e-9:
            self._planes[key] = base
            return base
        ent = self.part.NewEntity(self.c3.o3d_planeOffset)
        d = ent.GetDefinition()
        d.SetPlane(base)
        d.offset = abs(offset)
        d.direction = (offset * cal["so"]) > 0
        ent.hidden = True
        if not ent.Create():
            raise BuildError(f"не удалось создать смещённую плоскость {plane} {offset}")
        self._planes[key] = ent
        return ent

    def _sketch(self, plane: str, offset: float, contours: list[dict], axis_u: float | None = None,
                vrange=(-1000, 1000), angle: float | None = None, axis_v: float | None = None, urange=(-1000, 1000)):
        cal = self._cal(plane, angle is not None)
        su, sv = cal["su"], cal["sv"]
        swap = bool(cal.get("swap", 0))
        flip = (su * sv < 0) != swap  # отражение меняет направление дуг
        sk = self.part.NewEntity(self.c3.o3d_sketch)
        d = sk.GetDefinition()
        d.SetPlane(self._plane(plane, offset, angle))
        if not sk.Create():
            raise BuildError("не удалось создать эскиз")
        doc2d = d.BeginEdit()
        A = (lambda u, v: (sv * v, su * u)) if swap else (lambda u, v: (su * u, sv * v))  # noqa: E731
        for c in contours:
            if c["kind"] == "circle":
                (u, v), r = c["c"], c["r"]
                x, y = A(u, v)
                doc2d.ksCircle(x, y, r, 1)
                continue
            for seg in c["segs"]:
                if seg[0] == "L":
                    x1, y1 = A(*seg[1])
                    x2, y2 = A(*seg[2])
                    doc2d.ksLineSeg(x1, y1, x2, y2, 1)
                else:
                    _, cen, r, p1, p2, ccw = seg
                    xc, yc = A(*cen)
                    x1, y1 = A(*p1)
                    x2, y2 = A(*p2)
                    direction = 1 if (ccw != flip) else -1
                    doc2d.ksArcByPoint(xc, yc, r, x1, y1, x2, y2, direction, 1)
        if axis_u is not None:  # осевая линия (стиль 3) для вращения
            x1, y1 = A(axis_u, vrange[0])
            x2, y2 = A(axis_u, vrange[1])
            doc2d.ksLineSeg(x1, y1, x2, y2, 3)
        if axis_v is not None:  # горизонтальная осевая линия
            x1, y1 = A(urange[0], axis_v)
            x2, y2 = A(urange[1], axis_v)
            doc2d.ksLineSeg(x1, y1, x2, y2, 3)
        d.EndEdit()
        return sk

    def _rotated_sketch(self, p: dict):
        """Эскиз на вертикальной плоскости, повёрнутой вокруг Z.
        Модель КОМПАСа (проверено kompas_rotdiag): плоскость под углом k к XOZ/YOZ вокруг OZ имеет
        нормаль nK = R(k)·n0 (n0 = +Y для XOZ, −X для YOZ); ось x эскиза aK — проекция глобальной X
        на плоскость (если X ⟂ плоскости — то −Z); ось y эскиза bK = nK × aK; смещение direction=True — вдоль +nK;
        выдавливание в прямом направлении — вдоль +nK, вырез — наоборот."""
        from .refgeom import vframe
        O, e, n = vframe(p)
        k = p["rot"]["angle"] % 180.0
        kr = math.radians(k)
        n0 = (0.0, 1.0) if p["plane"] == "XZ" else (-1.0, 0.0)
        nK = (math.cos(kr) * n0[0] - math.sin(kr) * n0[1], math.sin(kr) * n0[0] + math.cos(kr) * n0[1], 0.0)
        hx, hy = 1 - nK[0] * nK[0], -nK[0] * nK[1]          # проекция X на плоскость
        hl = math.hypot(hx, hy)
        aK = (hx / hl, hy / hl, 0.0) if hl > 1e-6 else (0.0, 0.0, -1.0)
        bK = (nK[1] * aK[2] - nK[2] * aK[1], nK[2] * aK[0] - nK[0] * aK[2], nK[0] * aK[1] - nK[1] * aK[0])
        sig = 1.0 if (n[0] * nK[0] + n[1] * nK[1]) > 0 else -1.0
        W = O[0] * n[0] + O[1] * n[1] + p["offset"]           # положение нашей плоскости вдоль n
        dot = lambda P, Q: P[0] * Q[0] + P[1] * Q[1] + P[2] * Q[2]  # noqa: E731
        e3, z3 = (e[0], e[1], 0.0), (0.0, 0.0, 1.0)
        M = [[dot(e3, aK), dot(z3, aK)], [dot(e3, bK), dot(z3, bK)]]
        a0, b0 = O[0] * aK[0] + O[1] * aK[1], O[0] * bK[0] + O[1] * bK[1]
        flip = (M[0][0] * M[1][1] - M[0][1] * M[1][0]) < 0

        def mp(q):
            u, v = q
            return (a0 + M[0][0] * u + M[0][1] * v, b0 + M[1][0] * u + M[1][1] * v)

        # плоскость: под углом k через OZ, затем смещение вдоль nK
        key = ("rot", round(k, 9), round(sig * W, 9))
        if key in self._planes:
            plane = self._planes[key]
        else:
            base = self.part.GetDefaultEntity(getattr(self.c3, PLANE_ENTITY[p["plane"]]))
            ap = self.part.NewEntity(self.c3.o3d_planeAngle)
            d = ap.GetDefinition()
            d.SetPlane(base)
            d.SetAxis(self.part.GetDefaultEntity(self.c3.o3d_axisOZ))
            d.angle = float(k)
            ap.hidden = True
            if not ap.Create():
                raise BuildError(f"не удалось создать плоскость под углом {k}° к {p['plane']}")
            plane = ap
            dist = sig * W
            if abs(dist) > 1e-9:
                op = self.part.NewEntity(self.c3.o3d_planeOffset)
                od = op.GetDefinition()
                od.SetPlane(ap)
                od.offset = abs(dist)
                od.direction = dist > 0
                op.hidden = True
                if not op.Create():
                    raise BuildError("не удалось сместить плоскость под углом")
                plane = op
            self._planes[key] = plane
        sk = self.part.NewEntity(self.c3.o3d_sketch)
        sd = sk.GetDefinition()
        sd.SetPlane(plane)
        if not sk.Create():
            raise BuildError("не удалось создать эскиз на плоскости под углом")
        doc2d = sd.BeginEdit()
        for c in p["contours"]:
            if c["kind"] == "circle":
                x, y = mp(c["c"])
                doc2d.ksCircle(x, y, c["r"], 1)
                continue
            for sg in c["segs"]:
                if sg[0] == "L":
                    (x1, y1), (x2, y2) = mp(sg[1]), mp(sg[2])
                    doc2d.ksLineSeg(x1, y1, x2, y2, 1)
                else:
                    _, cen, r, p1, p2, ccw = sg
                    (xc, yc), (x1, y1), (x2, y2) = mp(cen), mp(p1), mp(p2)
                    doc2d.ksArcByPoint(xc, yc, r, x1, y1, x2, y2, 1 if (ccw != flip) else -1, 1)
        sd.EndEdit()
        sn = sig * (-1.0 if p["mode"] == "cut" else 1.0)
        return sk, sn

    # ------------------------------------------------------------ операции
    def _side(self, dist):
        if math.isinf(dist):
            return self.c3.etThroughAll, 0.0
        return self.c3.etBlind, float(dist)

    @staticmethod
    def _shifted(p: dict) -> dict:
        """Диапазон не включает плоскость эскиза (например, слой z от 7,5 до 10) — переносим плоскость."""
        if p["d0"] <= 0 <= p["d1"]:
            return p
        sh = p["d0"] if p["d0"] > 0 else p["d1"]
        if p.get("frame"):
            fr = p["frame"]
            O = tuple(fr["O"][i] + sh * fr["n"][i] for i in range(3))
            return {**p, "frame": {**fr, "O": O}, "d0": p["d0"] - sh, "d1": p["d1"] - sh}
        return {**p, "offset": p["offset"] + sh, "d0": p["d0"] - sh, "d1": p["d1"] - sh}

    def prism(self, p: dict):
        p = self._shifted(p)
        d0, d1 = p["d0"], p["d1"]
        if p.get("frame"):
            sk, sn = self._frame_sketch(p["frame"], p["contours"], p["mode"])
        elif p.get("rot"):
            sk, sn = self._rotated_sketch(p)
        else:
            sk = self._sketch(p["plane"], p["offset"], p["contours"])
            cal = self._cal(p["plane"])
            sn = cal["sn"] * (cal.get("sc", -1) if p["mode"] == "cut" else 1)
        if not (d0 <= 0 <= d1):
            raise BuildError("диапазон выдавливания должен включать плоскость эскиза")
        pos, neg = d1, -d0  # вдоль +w и −w
        normal, reverse = (pos, neg) if sn > 0 else (neg, pos)
        if p["mode"] == "add":
            kind = self.c3.o3d_bossExtrusion if self._has_body else self.c3.o3d_baseExtrusion
        else:
            kind = self.c3.o3d_cutExtrusion
        ent = self.part.NewEntity(kind)
        d = ent.GetDefinition()
        if p["mode"] == "cut":
            try:
                d.cut = True
            except Exception:
                pass
        if normal > 0 and reverse > 0:
            d.directionType = self.c3.dtBoth
            t, v = self._side(normal)
            d.SetSideParam(True, t, v, 0, False)
            t, v = self._side(reverse)
            d.SetSideParam(False, t, v, 0, False)
        elif normal > 0:
            d.directionType = self.c3.dtNormal
            t, v = self._side(normal)
            d.SetSideParam(True, t, v, 0, False)
        else:
            d.directionType = self.c3.dtReverse
            t, v = self._side(reverse)
            d.SetSideParam(False, t, v, 0, False)
        d.SetSketch(sk)
        if not ent.Create():
            raise BuildError(f"операция выдавливания ({p['mode']}, {p['plane']}) не выполнена")
        if p["mode"] == "add":
            self._has_body = True

    def _profile_loop(self, p: dict, mp, flip: bool) -> dict:
        """Профиль тела вращения в координатах (r, z) → контур эскиза через отображение mp(r, z) → (u, v)."""
        prof = p["profile"]
        if p.get("ploop"):
            src = p["ploop"]
        elif p.get("pcircle"):
            R, zc, rr = p["pcircle"]
            return {"kind": "circle", "c": mp(R, zc), "r": rr}
        else:
            src = {"kind": "loop", "segs": [("L", prof[i], prof[(i + 1) % len(prof)]) for i in range(len(prof))]}
        segs = []
        for g in src["segs"]:
            if g[0] == "L":
                segs.append(("L", mp(*g[1]), mp(*g[2])))
            else:
                segs.append(("A", mp(*g[1]), g[2], mp(*g[3]), mp(*g[4]), (not g[5]) if flip else g[5]))
        return {"kind": "loop", "segs": segs}

    def revolve(self, p: dict):
        prof = p["profile"]
        zs = [z for _, z in prof]
        ax = p.get("axis", "Z")
        if ax == "X":  # плоскость XY на высоте cz; u = X (вдоль оси), v = Y; ось — прямая v = cy
            loop = self._profile_loop(p, lambda r, z: (z, p["cy"] + r), True)
            sk = self._sketch("XY", p.get("cz", 0.0), [loop], axis_v=p["cy"], urange=(min(zs) - 5, max(zs) + 5))
        elif ax == "Y":  # плоскость XY на высоте cz; u = X, v = Y (вдоль оси); ось — прямая u = cx
            loop = self._profile_loop(p, lambda r, z: (p["cx"] + r, z), False)
            sk = self._sketch("XY", p.get("cz", 0.0), [loop], axis_u=p["cx"], vrange=(min(zs) - 5, max(zs) + 5))
        else:  # плоскость XZ, смещённая на cy; u = X, v = Z
            loop = self._profile_loop(p, lambda r, z: (p["cx"] + r, z), False)
            sk = self._sketch("XZ", p["cy"], [loop], axis_u=p["cx"], vrange=(min(zs) - 5, max(zs) + 5))
        if p["mode"] == "add":
            kind = self.c3.o3d_bossRotated if self._has_body else self.c3.o3d_baseRotated
        else:
            kind = self.c3.o3d_cutRotated
        ent = self.part.NewEntity(kind)
        d = ent.GetDefinition()
        if p["mode"] == "cut":
            try:
                d.cut = True
            except Exception:
                pass
        try:
            d.toroidShellFlag = False
        except Exception:
            pass
        d.directionType = self.c3.dtNormal
        d.SetSideParam(True, 360.0)
        d.SetSketch(sk)
        if not ent.Create():
            raise BuildError(f"операция вращения ({p['mode']}) не выполнена")
        if p["mode"] == "add":
            self._has_body = True

    # ------------------------------------------------------------ плоскость произвольного положения
    # Строится через три точки (или «под углом» + «смещённая», если трёх точек нет в вашей версии).
    # Как КОМПАС расположит оси эскиза на такой плоскости, заранее неизвестно, поэтому один раз на плоскость
    # делается «пробная калибровка»: в скрытых черновых деталях выдавливаются крошечные квадратики
    # в точках эскиза (0;0), (10;0), (0;10) и в одну сторону — по их габаритам находятся начало,
    # оси эскиза и направление выдавливания в глобальных координатах.
    def _make_plane3(self, part, O, e, f, how: str):
        import numpy as np
        O, e, f = np.array(O), np.array(e), np.array(f)
        c3 = self.c3
        if how == "3points":
            ent = part.NewEntity(c3.o3d_plane3Points)
            d = ent.GetDefinition()
            for i, P in enumerate((O, O + 10 * e, O + 10 * f), start=1):
                d.SetPoint(i, float(P[0]), float(P[1]), float(P[2]))
            ent.hidden = True
            if not ent.Create():
                raise BuildError("плоскость через три точки не создана")
            return ent
        # how == "angle": базовая плоскость, содержащая ось поворота, + угол + смещение
        n = np.cross(e, f)
        best = None
        for base, bn in (("XY", (0, 0, 1)), ("XZ", (0, 1, 0)), ("YZ", (1, 0, 0))):
            for axn, av in (("o3d_axisOX", (1, 0, 0)), ("o3d_axisOY", (0, 1, 0)), ("o3d_axisOZ", (0, 0, 1))):
                av, bn_ = np.array(av, float), np.array(bn, float)
                if abs(av @ bn_) > 1e-9 or abs(av @ n) > 1e-6:  # ось должна лежать и в базовой, и в целевой плоскости
                    continue
                ang = math.degrees(math.atan2(np.cross(bn_, n) @ av, bn_ @ n))
                best = (base, axn, ang)
                break
            if best:
                break
        if best is None:
            raise BuildError("плоскость с двумя поворотами нельзя построить «под углом» — нужна плоскость через три точки")
        base, axn, ang = best
        sign = -1 if how == "angle-" else 1
        ap = part.NewEntity(c3.o3d_planeAngle)
        d = ap.GetDefinition()
        d.SetPlane(part.GetDefaultEntity(getattr(c3, PLANE_ENTITY[base])))
        d.SetAxis(part.GetDefaultEntity(getattr(c3, axn)))
        d.angle = float(sign * ang)
        ap.hidden = True
        if not ap.Create():
            raise BuildError("плоскость под углом не создана")
        dist = float(O @ n)
        if abs(dist) < 1e-9:
            return ap
        op = part.NewEntity(c3.o3d_planeOffset)
        od = op.GetDefinition()
        od.SetPlane(ap)
        od.offset = abs(dist)
        od.direction = dist > 0
        op.hidden = True
        if not op.Create():
            raise BuildError("смещённая плоскость не создана")
        return op

    def _probe(self, O, e, f, how, x, y, both=True):
        """Черновая деталь: квадратик 0,4 мм в точке эскиза (x, y) на плоскости → центр его габарита."""
        doc = self.k5.Document3D()
        doc.Create(True, True)
        try:
            part = doc.GetPart(self.c3.pTop_Part)
            pl = self._make_plane3(part, O, e, f, how)
            sk = part.NewEntity(self.c3.o3d_sketch)
            sd = sk.GetDefinition()
            sd.SetPlane(pl)
            sk.Create()
            d2 = sd.BeginEdit()
            h = 0.2
            for (a, b), (c, d_) in (((x - h, y - h), (x + h, y - h)), ((x + h, y - h), (x + h, y + h)),
                                    ((x + h, y + h), (x - h, y + h)), ((x - h, y + h), (x - h, y - h))):
                d2.ksLineSeg(a, b, c, d_, 1)
            sd.EndEdit()
            ex = part.NewEntity(self.c3.o3d_baseExtrusion)
            ed = ex.GetDefinition()
            if both:
                ed.directionType = self.c3.dtBoth
                ed.SetSideParam(True, self.c3.etBlind, 0.2, 0, False)
                ed.SetSideParam(False, self.c3.etBlind, 0.2, 0, False)
            else:
                ed.directionType = self.c3.dtNormal
                ed.SetSideParam(True, self.c3.etBlind, 1.0, 0, False)
            ed.SetSketch(sk)
            if not ex.Create():
                raise BuildError("пробное выдавливание не выполнено")
            g = part.GetGabarit(True, True)
            v = [t for t in g if isinstance(t, float)][-6:]
            return [(v[i] + v[i + 3]) / 2 for i in range(3)]
        finally:
            try:
                doc.close()
            except Exception:
                pass

    def _frame_calib(self, fr: dict):
        """Положение осей эскиза на наклонной плоскости: (как строить, O', e', f', n') — с кэшем."""
        import numpy as np
        O, e, f, n = (np.array(fr[k], float) for k in ("O", "e", "f", "n"))
        key = (tuple(np.round(n, 6)), round(float(O @ n), 6), tuple(np.round(e, 6)))
        cache = getattr(self, "_frame_cache", None)
        if cache is None:
            cache = self._frame_cache = {}
        if key in cache:
            return cache[key]
        errs = []
        for how in ([self.calib["frame_how"]] if self.calib.get("frame_how") else ["3points", "angle", "angle-"]):
            try:
                c0 = np.array(self._probe(O, e, f, how, 0, 0))
                cx = np.array(self._probe(O, e, f, how, 10, 0))
                cy = np.array(self._probe(O, e, f, how, 0, 10))
                cn = np.array(self._probe(O, e, f, how, 0, 0, both=False))
            except Exception as ex:
                errs.append(f"{how}: {type(ex).__name__}: {ex}")
                continue
            e2, f2, n2 = (cx - c0) / 10, (cy - c0) / 10, (cn - c0) / 0.5
            if not all(abs(np.linalg.norm(v) - 1) < 2e-3 for v in (e2, f2, n2)):
                errs.append(f"{how}: оси эскиза не единичные {e2}, {f2}, {n2}")
                continue
            if abs(abs(n2 @ n) - 1) > 2e-3 or abs((O - c0) @ n) > 2e-2:
                errs.append(f"{how}: плоскость легла не туда (нормаль {np.round(n2, 4)}, нужна ±{np.round(n, 4)})")
                continue
            cache[key] = (how, c0, e2, f2, n2)
            return cache[key]
        raise BuildError("наклонная плоскость не построена: " + "; ".join(errs))

    def _frame_sketch(self, fr: dict, contours: list[dict], mode: str):
        import numpy as np
        how, Oc, ec, fc, nc = self._frame_calib(fr)
        O, e, f, n = (np.array(fr[k], float) for k in ("O", "e", "f", "n"))
        M = [[e @ ec, f @ ec], [e @ fc, f @ fc]]
        t0 = ((O - Oc) @ ec, (O - Oc) @ fc)
        flip = (M[0][0] * M[1][1] - M[0][1] * M[1][0]) < 0

        def mp(q):
            u, v = q
            return (t0[0] + M[0][0] * u + M[0][1] * v, t0[1] + M[1][0] * u + M[1][1] * v)

        key = ("frame",) + tuple(np.round(n, 6)) + (round(float(O @ n), 6),) + tuple(np.round(e, 6))
        plane = self._planes.get(key)
        if plane is None:
            plane = self._planes[key] = self._make_plane3(self.part, O, e, f, how)
        sk = self.part.NewEntity(self.c3.o3d_sketch)
        sd = sk.GetDefinition()
        sd.SetPlane(plane)
        if not sk.Create():
            raise BuildError("эскиз на наклонной плоскости не создан")
        d2 = sd.BeginEdit()
        for c in contours:
            if c["kind"] == "circle":
                x, y = mp(c["c"])
                d2.ksCircle(x, y, c["r"], 1)
                continue
            for g in c["segs"]:
                if g[0] == "L":
                    (x1, y1), (x2, y2) = mp(g[1]), mp(g[2])
                    d2.ksLineSeg(x1, y1, x2, y2, 1)
                else:
                    _, cen, r, p1, p2, ccw = g
                    (xc, yc), (x1, y1), (x2, y2) = mp(cen), mp(p1), mp(p2)
                    d2.ksArcByPoint(xc, yc, r, x1, y1, x2, y2, 1 if (ccw != flip) else -1, 1)
        sd.EndEdit()
        sig = 1.0 if (n @ nc) > 0 else -1.0
        sn = sig * (self.calib.get("frame_sc", -1) if mode == "cut" else 1)
        return sk, sn

    # ------------------------------------------------------------ уклон, по сечениям, кинематика
    def mesh(self, p: dict):
        b = p["build"]
        getattr(self, "_" + b["kind"])(p, b)

    def _draft(self, p: dict, b: dict):
        if b["tilts"]:
            from .meshgeom import apply_tilts, base_frame, frame_dict
            O, e, f, _ = base_frame(b["plane"], b["offset"])
            fr = frame_dict(*apply_tilts(O, e, f, b["tilts"]))
            sk, sn = self._frame_sketch(fr, [b["contour"]], p["mode"])
        else:
            sk = self._sketch(b["plane"], b["offset"], [b["contour"]])
            cal = self._cal(b["plane"])
            sn = cal["sn"] * (cal.get("sc", -1) if p["mode"] == "cut" else 1)
        along = 1 if b["direction"] == "+" else -1
        forward = (along * sn) > 0
        kind = (self.c3.o3d_bossExtrusion if self._has_body else self.c3.o3d_baseExtrusion) if p["mode"] == "add" \
            else self.c3.o3d_cutExtrusion
        ent = self.part.NewEntity(kind)
        d = ent.GetDefinition()
        if p["mode"] == "cut":
            try:
                d.cut = True
            except Exception:
                pass
        outward = bool(b["outward"]) != bool(self.calib.get("draft_flip", False))
        if p["mode"] == "cut":  # у выреза «наружу» относительно материала — наоборот
            outward = outward != bool(self.calib.get("draft_cut_flip", False))
        d.directionType = self.c3.dtNormal if forward else self.c3.dtReverse
        d.SetSideParam(forward, self.c3.etBlind, float(b["depth"]), float(b["angle"]), outward)
        d.SetSketch(sk)
        if not ent.Create():
            raise BuildError("выдавливание с уклоном не выполнено")
        if p["mode"] == "add":
            self._has_body = True

    def _loft(self, p: dict, b: dict):
        s0 = self._sketch(b["plane"], b["off0"], [b["c0"]])
        s1 = self._sketch(b["plane"], b["off1"], [b["c1"]])
        kind = (self.c3.o3d_bossLoft if self._has_body else self.c3.o3d_baseLoft) if p["mode"] == "add" \
            else self.c3.o3d_cutLoft
        ent = self.part.NewEntity(kind)
        d = ent.GetDefinition()
        if p["mode"] == "cut":
            try:
                d.cut = True
            except Exception:
                pass
        arr = d.Sketchs()
        arr.Add(s0)
        arr.Add(s1)
        if not ent.Create():
            raise BuildError("операция по сечениям не выполнена")
        if p["mode"] == "add":
            self._has_body = True

    def _sweep(self, p: dict, b: dict):
        from .meshgeom import base_frame
        segs = b["segs"]
        path = self._sketch(b["plane"], b["offset"], [{"kind": "loop", "segs": segs}])
        O, e, f, _ = base_frame(b["plane"], b["offset"])
        g = segs[0]
        P0 = g[1] if g[0] == "L" else g[3]
        P1 = g[2] if g[0] == "L" else g[4]
        S = O + P0[0] * e + P0[1] * f
        dirv = (P1[0] - P0[0]) * e + (P1[1] - P0[1]) * f
        if g[0] != "L":
            raise BuildError("траектория должна начинаться с прямого участка")
        ax = int(max(range(3), key=lambda i: abs(dirv[i])))
        sec_plane = {0: "YZ", 1: "XZ", 2: "XY"}[ax]
        uv = {"YZ": (S[1], S[2]), "XZ": (S[0], S[2]), "XY": (S[0], S[1])}[sec_plane]
        circles = [{"kind": "circle", "c": uv, "r": b["r"]}]
        if b["ri"] > 0:
            circles.append({"kind": "circle", "c": uv, "r": b["ri"]})
        sec = self._sketch(sec_plane, float(S[ax]), circles)
        kind = (self.c3.o3d_bossEvolution if self._has_body else self.c3.o3d_baseEvolution) if p["mode"] == "add" \
            else self.c3.o3d_cutEvolution
        ent = self.part.NewEntity(kind)
        d = ent.GetDefinition()
        if p["mode"] == "cut":
            try:
                d.cut = True
            except Exception:
                pass
        shift = self.calib.get("sweep", {}).get("shift", self.calib.get("spring", {}).get("shift", 1))
        try:
            d.sketchShiftType = shift
        except Exception:
            pass
        d.SetSketch(sec)
        arr = d.PathPartArray()
        arr.Add(path)
        if not ent.Create():
            raise BuildError("кинематическая операция (труба) не выполнена")
        if p["mode"] == "add":
            self._has_body = True

    # ---- условное изображение резьбы (не меняет геометрию; ошибка не роняет деталь)
    def _find_cyl_face(self, r: float, cx: float, cy: float, zmid: float):
        coll = self.part.EntityCollection(self.c3.o3d_face)
        best = None
        for i in range(coll.GetCount()):
            f = coll.GetByIndex(i)
            fd = f.GetDefinition()
            try:
                if not fd.IsCylinder():
                    continue
                res = fd.GetCylinderParam()
                vals = [v for v in (res if isinstance(res, (list, tuple)) else [res]) if isinstance(v, float)]
                rr = vals[-1] if vals else None
            except Exception:
                rr = None
            if rr is not None and abs(rr - r) > 0.01:
                continue
            # проверка положения по габариту грани
            try:
                g = f.GetGabarit(True, True) if hasattr(f, "GetGabarit") else fd.GetGabarit()
                gv = [v for v in g if isinstance(v, float)][-6:]
                x0, y0, z0, x1, y1, z1 = gv
                if abs((x0 + x1) / 2 - cx) > 0.05 or abs((y0 + y1) / 2 - cy) > 0.05 or not (z0 - 0.01 <= zmid <= z1 + 0.01):
                    continue
            except Exception:
                pass
            best = f
            break
        return best

    def thread(self, p: dict):
        try:
            face = self._find_cyl_face(p["d"] / 2, p["cx"], p["cy"], (p["z0"] + p["z1"]) / 2)
            if face is None:
                print(f"   ! резьба М{p['d']:g}: не найдена цилиндрическая грань Ø{p['d']:g}")
                return
            ent = self.part.NewEntity(self.c3.o3d_thread)
            td = ent.GetDefinition()
            td.SetBaseObject(face)
            for attr, val in (("autoDefinDr", False), ("dr", float(p["d"])), ("p", float(p["pitch"])),
                              ("allLength", False), ("length", float(p["z1"] - p["z0"]))):
                try:
                    setattr(td, attr, val)
                except Exception:
                    pass
            if not ent.Create():
                print(f"   ! резьба М{p['d']:g}: операция не выполнена")
        except Exception as e:
            print(f"   ! резьба М{p['d']:g}: {type(e).__name__}: {e}")

    # ---- пружина: цилиндрическая спираль + кинематическая операция
    def helix(self, p: dict):
        c3, cal = self.c3, self.calib.get("spring", {})
        R, r, pitch, n = p["R"], p["r"], p["pitch"], p["turns"]
        sp = self.part.NewEntity(c3.o3d_cylindricSpiral)
        d = sp.GetDefinition()
        d.SetPlane(self._plane("XY", p["z0"]))
        for attr, val in (("buildMode", cal.get("buildMode", 0)), ("diamType", 0), ("diam", 2 * R),
                          ("step", pitch), ("turn", n), ("height", pitch * n),
                          ("buildDir", bool(cal.get("buildDir", True))), ("turnDir", True), ("firstAngle", 0.0)):
            try:
                setattr(d, attr, val)
            except Exception:
                pass
        try:
            d.SetLocation(float(p["cx"]), float(p["cy"]))
        except Exception:
            pass
        sp.hidden = True
        if not sp.Create():
            raise BuildError("не удалось создать цилиндрическую спираль")
        # сечение: круг в плоскости XZ (через ось), центр — начало спирали
        sk = self._sketch("XZ", p["cy"], [{"kind": "circle", "c": (p["cx"] + R, p["z0"]), "r": r}])
        ev = self.part.NewEntity(c3.o3d_baseEvolution if not self._has_body else c3.o3d_bossEvolution)
        ed = ev.GetDefinition()
        try:
            ed.sketchShiftType = cal.get("shift", 1)
        except Exception:
            pass
        ed.SetSketch(sk)
        arr = ed.PathPartArray()
        arr.Add(sp)
        if not ev.Create():
            raise BuildError("кинематическая операция (пружина) не выполнена")
        self._has_body = True

    # ---- рёбра (экспериментально: проверьте на selftest)
    def _edges(self, sel: dict, tol=1e-3):
        coll = self.part.EntityCollection(self.c3.o3d_edge)
        out = []
        for i in range(coll.GetCount()):
            e = coll.GetByIndex(i)
            ed = e.GetDefinition()
            pts = []
            for begin in (True, False):
                try:
                    v = ed.GetVertex(begin)
                    r = v.GetDefinition().GetPoint()
                    pts.append(tuple(r[-3:]))
                except Exception:
                    pass
            straight = bool(ed.IsStraight())
            t = sel.get("type", "any")
            if t == "line" and not straight:
                continue
            if t == "circle" and straight:
                continue
            if sel.get("z") is not None and pts:
                if not all(abs(pz - sel["z"]) < tol for _, _, pz in pts):
                    continue
            if t == "line" and sel.get("dir") and len(pts) == 2:
                ax = "XYZ".index(sel["dir"].upper())
                d = [pts[1][k] - pts[0][k] for k in range(3)]
                if any(abs(d[k]) > tol for k in range(3) if k != ax):
                    continue
            if t == "circle" and sel.get("radius") is not None:
                try:
                    g = ed.GetCurve3D().GetGabarit()
                    vals = [x for x in g if isinstance(x, float)]
                    rr = max(vals[3] - vals[0], vals[4] - vals[1]) / 2
                    if abs(rr - sel["radius"]) > 1e-2:
                        continue
                except Exception:
                    pass
            out.append(e)
        return out

    def edge(self, p: dict):
        edges = self._edges(p["edges"])
        if not edges:
            raise BuildError(f"{p['kind']}: не найдено рёбер по селектору {p['edges']}")
        if p["kind"] == "fillet":
            ent = self.part.NewEntity(self.c3.o3d_fillet)
            d = ent.GetDefinition()
            d.radius = p["size"]
        else:
            ent = self.part.NewEntity(self.c3.o3d_chamfer)
            d = ent.GetDefinition()
            d.SetChamferParam(True, p["size"], p["size"])
        arr = d.array()
        for e in edges:
            arr.Add(e)
        if not ent.Create():
            raise BuildError(f"{p['kind']} не выполнено")

    # ------------------------------------------------------------ сборка и измерение
    def build(self, prims: list[dict]):
        # наклонные плоскости калибруются в черновых деталях ДО создания основной
        for i, p in enumerate(prims):
            fr = self._shifted(p).get("frame") if p["type"] == "prism" else None
            if p["type"] == "mesh" and p["build"].get("tilts"):
                from .meshgeom import apply_tilts, base_frame, frame_dict
                b = p["build"]
                O, e, f, _ = base_frame(b["plane"], b["offset"])
                fr = frame_dict(*apply_tilts(O, e, f, b["tilts"]))
            if fr:
                try:
                    self._frame_calib(fr)
                except BuildError as e:
                    raise BuildError(f"примитив {i}: {e}") from None
        self.new_part()
        for i, p in enumerate(prims):
            try:
                getattr(self, {"prism": "prism", "revolve": "revolve", "edge": "edge", "helix": "helix", "thread": "thread",
                         "mesh": "mesh"}[p["type"]])(p)
            except BuildError as e:
                raise BuildError(f"примитив {i}: {e}") from None
            except Exception as e:  # COM-ошибки
                raise BuildError(f"примитив {i} ({p['type']}): {type(e).__name__}: {e}") from None

    def measure(self) -> dict:
        res = {"volume": None, "bbox": None}
        # габарит
        try:
            g = self.part.GetGabarit(True, True)
            vals = [x for x in g if isinstance(x, float)][-6:]
            x1, y1, z1, x2, y2, z2 = vals
            res["bbox"] = [abs(x2 - x1), abs(y2 - y1), abs(z2 - z1)]
        except Exception as e:
            res["bbox_error"] = repr(e)
        # объём: несколько способов, какой сработает в вашей версии
        vol = None
        try:
            bits = getattr(self.c5, "ST_MIX_MM", 0) | getattr(self.c5, "ST_MIX_KG", 0)
            mip = self.part.CalcMassInertiaProperties(bits)
            for attr in ("v", "V", "volume", "Volume"):
                if hasattr(mip, attr):
                    vol = float(getattr(mip, attr))
                    break
        except Exception as e:
            res["volume_error_api5"] = repr(e)
        if vol is None:
            try:
                doc7 = self.app7.ActiveDocument
                part7 = self.api7.IKompasDocument3D(doc7).TopPart
                mi = self.api7.IMassInertiaParam7(part7)
                mi.Calculate()
                vol = float(mi.Volume)
            except Exception as e:
                res["volume_error_api7"] = repr(e)
        if vol is not None:
            res["volume"] = vol * float(self.calib.get("volume_scale", 1.0))
        if res["bbox"]:
            res["bbox_sorted"] = sorted(res["bbox"])
        return res
