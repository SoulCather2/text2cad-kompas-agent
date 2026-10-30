"""
Мини-язык операций (DSL), который выдаёт LLM, и его нормализация в примитивы.

LLM пишет высокоуровневые операции (extrude, holes, shaft, revolve, fillet, chamfer).
normalize() проверяет их и раскладывает на 3 вида примитивов, которые умеют
исполнители (refgeom — независимая проверка, kompas_exec — КОМПАС-3D):

  {"type": "prism",   "mode": "add"|"cut", "plane": "XY"|"XZ"|"YZ", "offset": w0,
   "contours": [...], "d0": float, "d1": float}     # тело между w0+d0 и w0+d1 по нормали
  {"type": "revolve", "mode": ..., "profile": [(r, z), ...], "cx": .., "cy": ..}  # вокруг оси || Z
  {"type": "edge",    "kind": "fillet"|"chamfer", "size": .., "edges": {...}}

Контур:  {"kind": "circle", "c": (u, v), "r": r}
         {"kind": "loop", "segs": [("L", p1, p2) | ("A", c, r, p1, p2, ccw), ...]}

Системы координат эскиза (u, v) и нормаль w:
  XY: u=X, v=Y, w=Z     XZ: u=X, v=Z, w=Y     YZ: u=Y, v=Z, w=X
"""
from __future__ import annotations

import math
from typing import Any

INF = float("inf")
PLANES = ("XY", "XZ", "YZ")
EPS = 1e-9


class DSLError(ValueError):
    """Ошибка в спецификации. Текст уходит обратно в LLM для исправления."""


# ---------------------------------------------------------------- проверки
def _num(v: Any, name: str, *, min_: float | None = None, gt: float | None = None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise DSLError(f"{name}: ожидается число, получено {v!r}")
    v = float(v)
    if min_ is not None and v < min_:
        raise DSLError(f"{name}: должно быть ≥ {min_}, получено {v}")
    if gt is not None and v <= gt:
        raise DSLError(f"{name}: должно быть > {gt}, получено {v}")
    return v


def _get(d: dict, key: str, default: Any = None, *, required: bool = False, where: str = "") -> Any:
    if key in d and d[key] is not None:
        return d[key]
    if required:
        raise DSLError(f"{where}: нет обязательного поля '{key}'")
    return default


def _pt(p: Any, name: str) -> tuple[float, float]:
    if not isinstance(p, (list, tuple)) or len(p) != 2:
        raise DSLError(f"{name}: точка должна быть [x, y], получено {p!r}")
    return (_num(p[0], name), _num(p[1], name))


# ---------------------------------------------------------------- фигуры эскиза
def _loop_from_points(pts: list[tuple[float, float]]) -> dict:
    segs = []
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        if math.dist(a, b) > EPS:
            segs.append(("L", a, b))
    return {"kind": "loop", "segs": segs}


def _polygon_area(pts) -> float:
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return s / 2


def shape_to_contour(sh: dict, where: str) -> dict:
    if not isinstance(sh, dict):
        raise DSLError(f"{where}: фигура должна быть объектом, получено {sh!r}")
    t = sh.get("type")
    w = f"{where} ({t})"
    if t == "circle":
        d = _num(_get(sh, "d", required=True, where=w), w + ".d", gt=0)
        return {"kind": "circle", "c": (_num(sh.get("cx", 0), w + ".cx"), _num(sh.get("cy", 0), w + ".cy")), "r": d / 2}

    if t == "rect":
        W = _num(_get(sh, "w", required=True, where=w), w + ".w", gt=0)
        H = _num(_get(sh, "h", required=True, where=w), w + ".h", gt=0)
        if "x0" in sh or "y0" in sh:
            cx = _num(sh.get("x0", 0), w + ".x0") + W / 2
            cy = _num(sh.get("y0", 0), w + ".y0") + H / 2
        else:
            cx, cy = _num(sh.get("cx", 0), w + ".cx"), _num(sh.get("cy", 0), w + ".cy")
        r = _num(sh.get("r", 0) or 0, w + ".r", min_=0)
        if r > min(W, H) / 2 + EPS:
            raise DSLError(f"{w}: радиус скругления r={r} больше половины меньшей стороны")
        r = min(r, min(W, H) / 2)
        x0, x1, y0, y1 = cx - W / 2, cx + W / 2, cy - H / 2, cy + H / 2
        if r <= EPS:
            return _loop_from_points([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
        segs = []

        def L(a, b):
            if math.dist(a, b) > EPS:
                segs.append(("L", a, b))

        L((x0 + r, y0), (x1 - r, y0))
        segs.append(("A", (x1 - r, y0 + r), r, (x1 - r, y0), (x1, y0 + r), True))
        L((x1, y0 + r), (x1, y1 - r))
        segs.append(("A", (x1 - r, y1 - r), r, (x1, y1 - r), (x1 - r, y1), True))
        L((x1 - r, y1), (x0 + r, y1))
        segs.append(("A", (x0 + r, y1 - r), r, (x0 + r, y1), (x0, y1 - r), True))
        L((x0, y1 - r), (x0, y0 + r))
        segs.append(("A", (x0 + r, y0 + r), r, (x0, y0 + r), (x0 + r, y0), True))
        return {"kind": "loop", "segs": segs}

    if t == "polygon":
        pts = _get(sh, "points", required=True, where=w)
        if not isinstance(pts, list) or len(pts) < 3:
            raise DSLError(f"{w}: нужно ≥3 точек")
        pts = [_pt(p, w + ".points") for p in pts]
        if math.dist(pts[0], pts[-1]) < EPS:
            pts = pts[:-1]
        if abs(_polygon_area(pts)) < EPS:
            raise DSLError(f"{w}: многоугольник нулевой площади")
        return _loop_from_points(pts)

    if t in ("regular_polygon", "hexagon"):
        n = int(sh.get("n", 6)) if t == "regular_polygon" else 6
        if n < 3:
            raise DSLError(f"{w}: n должно быть ≥3")
        cx, cy = _num(sh.get("cx", 0), w + ".cx"), _num(sh.get("cy", 0), w + ".cy")
        if sh.get("s") is not None:  # размер под ключ (между гранями)
            R = _num(sh["s"], w + ".s", gt=0) / 2 / math.cos(math.pi / n)
        elif sh.get("d") is not None:  # диаметр описанной окружности (по вершинам)
            R = _num(sh["d"], w + ".d", gt=0) / 2
        else:
            raise DSLError(f"{w}: задайте s (под ключ, между гранями) или d (по вершинам)")
        a0 = math.radians(_num(sh.get("angle", 0), w + ".angle"))
        pts = [(cx + R * math.cos(a0 + 2 * math.pi * k / n), cy + R * math.sin(a0 + 2 * math.pi * k / n)) for k in range(n)]
        return _loop_from_points(pts)

    if t == "slot":  # паз со скруглёнными концами; length — полная длина
        Lg = _num(_get(sh, "length", required=True, where=w), w + ".length", gt=0)
        W = _num(_get(sh, "width", required=True, where=w), w + ".width", gt=0)
        if Lg < W - EPS:
            raise DSLError(f"{w}: length (полная длина) меньше width")
        cx, cy = _num(sh.get("cx", 0), w + ".cx"), _num(sh.get("cy", 0), w + ".cy")
        a = math.radians(_num(sh.get("angle", 0), w + ".angle"))
        r, hl = W / 2, (Lg - W) / 2
        if hl < EPS:
            return {"kind": "circle", "c": (cx, cy), "r": r}
        ux, uy, nx, ny = math.cos(a), math.sin(a), -math.sin(a), math.cos(a)
        c1 = (cx - ux * hl, cy - uy * hl)
        c2 = (cx + ux * hl, cy + uy * hl)
        p1 = (c1[0] - nx * r, c1[1] - ny * r)
        p2 = (c2[0] - nx * r, c2[1] - ny * r)
        p3 = (c2[0] + nx * r, c2[1] + ny * r)
        p4 = (c1[0] + nx * r, c1[1] + ny * r)
        return {"kind": "loop", "segs": [("L", p1, p2), ("A", c2, r, p2, p3, True),
                                         ("L", p3, p4), ("A", c1, r, p4, p1, True)]}

    if t == "arc_slot":  # дуговой паз: средний радиус r, ширина width, от угла a1 до a2 (град, против часовой)
        cx, cy = _num(sh.get("cx", 0), w + ".cx"), _num(sh.get("cy", 0), w + ".cy")
        R = _num(_get(sh, "r", required=True, where=w), w + ".r", gt=0)
        W = _num(_get(sh, "width", required=True, where=w), w + ".width", gt=0)
        if W >= 2 * R:
            raise DSLError(f"{w}: ширина паза должна быть меньше диаметра средней линии")
        a1 = math.radians(_num(_get(sh, "a1", required=True, where=w), w + ".a1"))
        a2 = math.radians(_num(_get(sh, "a2", required=True, where=w), w + ".a2"))
        if a2 <= a1:
            a2 += 2 * math.pi
        if a2 - a1 >= 2 * math.pi - 1e-6:
            raise DSLError(f"{w}: дуга должна быть меньше полной окружности")
        ro, ri, rc = R + W / 2, R - W / 2, W / 2
        P = lambda r, a: (cx + r * math.cos(a), cy + r * math.sin(a))  # noqa: E731
        return {"kind": "loop", "segs": [
            ("A", (cx, cy), ro, P(ro, a1), P(ro, a2), True),
            ("A", P(R, a2), rc, P(ro, a2), P(ri, a2), True),
            ("A", (cx, cy), ri, P(ri, a2), P(ri, a1), False),
            ("A", P(R, a1), rc, P(ri, a1), P(ro, a1), True),
        ]}

    raise DSLError(f"{where}: неизвестный тип фигуры {t!r} (есть: rect, circle, polygon, regular_polygon, hexagon, slot, arc_slot)")


# ---------------------------------------------------------------- операции
def _range(depth, direction: str, where: str) -> tuple[float, float]:
    direction = direction or "+"
    if depth == "through":
        return {"+": (0, INF), "-": (-INF, 0), "both": (-INF, INF)}[direction]
    D = _num(depth, where + ".depth", gt=0)
    if direction == "+":
        return (0.0, D)
    if direction == "-":
        return (-D, 0.0)
    if direction == "both":
        return (-D / 2, D / 2)
    raise DSLError(f"{where}: direction должно быть '+', '-' или 'both'")


def _mode(op: dict, where: str, default="add") -> str:
    m = op.get("mode", default)
    if m not in ("add", "cut"):
        raise DSLError(f"{where}: mode должно быть 'add' или 'cut'")
    return m


def _plane(op: dict, where: str) -> str:
    p = str(op.get("plane", "XY")).upper()
    if p not in PLANES:
        raise DSLError(f"{where}: plane должно быть одним из {PLANES}")
    return p


def _expand_extrude(op: dict, where: str) -> list[dict]:
    sk = _get(op, "sketch", required=True, where=where)
    if isinstance(sk, dict):
        sk = [sk]
    if not isinstance(sk, list) or not sk:
        raise DSLError(f"{where}: sketch — список фигур")
    mode = _mode(op, where)
    direction = op.get("direction", "+")
    depth = _get(op, "depth", required=True, where=where)
    if depth == "through" and mode == "add":
        raise DSLError(f"{where}: depth='through' допустим только для mode='cut'")
    d0, d1 = _range(depth, direction, where)
    plane, off = _plane(op, where), _num(op.get("offset", 0), where + ".offset")
    contours = [shape_to_contour(s, f"{where}.sketch[{i}]") for i, s in enumerate(sk)]
    tilts = _tilts(op, where)
    if op.get("draft") not in (None, 0, 0.0):
        return [_draft_mesh(op, where, plane, off, contours, mode, depth, direction, tilts)]
    prim = {"type": "prism", "mode": mode, "plane": plane, "offset": off, "contours": contours, "d0": d0, "d1": d1}
    return [_tilted(prim, tilts)] if tilts else [prim]


# ---------------------------------------------------------------- наклонные плоскости, уклон, кинематика, сечения
def _tilts(op: dict, where: str) -> list[dict]:
    """tilt: {"axis": "X"|"Y"|"Z", "angle": град, "point": [x, y, z]} или список таких поворотов (по порядку)."""
    t = op.get("tilt")
    if t is None:
        return []
    lst = t if isinstance(t, list) else [t]
    out = []
    for i, x in enumerate(lst):
        w = f"{where}.tilt[{i}]"
        if not isinstance(x, dict):
            raise DSLError(f"{w}: поворот — объект {{axis, angle, point}}")
        ax = str(x.get("axis", "")).upper()
        if ax not in ("X", "Y", "Z"):
            raise DSLError(f"{w}: axis — X, Y или Z (ось поворота плоскости)")
        pt = x.get("point", [0, 0, 0])
        if not isinstance(pt, list) or len(pt) != 3:
            raise DSLError(f"{w}: point — [x, y, z] точки на оси поворота")
        out.append({"axis": ax, "angle": _num(_get(x, "angle", required=True, where=w), w + ".angle"),
                    "point": [_num(v, w + ".point") for v in pt]})
    return out


def _frame_of(plane: str, off: float, tilts: list[dict]) -> dict:
    from .meshgeom import apply_tilts, base_frame, frame_dict
    O, e, f, n = base_frame(plane, off)
    return frame_dict(*apply_tilts(O, e, f, tilts))


def _tilted(prim: dict, tilts: list[dict]) -> dict:
    """Призма на плоскости, повёрнутой вместе с эскизом вокруг осей tilts."""
    if prim["type"] != "prism" or prim.get("rot") or prim.get("frame"):
        raise DSLError("tilt применим только к выдавливанию, вырезу и отверстиям")
    return {**prim, "frame": _frame_of(prim["plane"], prim["offset"], tilts), "offset": 0.0, "tilts": tilts}


def _draft_mesh(op, where, plane, off, contours, mode, depth, direction, tilts) -> dict:
    """Выдавливание с уклоном: сечение равномерно сужается (draft_out: true — расширяется) на угол draft."""
    from .meshgeom import apply_tilts, base_frame, offset_contour, ruled_mesh, sample_contour, seg_counts
    if depth == "through" or direction not in ("+", "-"):
        raise DSLError(f"{where}: уклон — только с числовой глубиной и direction '+' или '-'")
    if len(contours) != 1:
        raise DSLError(f"{where}: уклон — только для одного контура в эскизе (отверстия добавьте отдельной операцией)")
    ang = _num(op["draft"], where + ".draft", gt=0)
    if ang >= 45:
        raise DSLError(f"{where}: угол уклона должен быть меньше 45°")
    D = _num(depth, where + ".depth", gt=0)
    out = bool(op.get("draft_out", False))
    delta = D * math.tan(math.radians(ang)) * (-1 if out else 1)
    c0 = contours[0]
    try:
        c1 = offset_contour(c0, delta)
    except ValueError as e:
        raise DSLError(f"{where}: уклон {ang}° на глубине {D}: {e}") from None
    k = seg_counts(c0)
    P0, P1 = sample_contour(c0, k), sample_contour(c1, k)
    O, e, f, _ = base_frame(plane, off)
    O, e, f, n = apply_tilts(O, e, f, tilts)
    w1 = D if direction == "+" else -D
    V, F, caps, vol = ruled_mesh(P0, P1, O, e, f, n, 0.0, w1)
    return {"type": "mesh", "mode": mode, "V": V, "F": F, "caps": caps, "vexact": vol, "approx": False,
            "build": {"kind": "draft", "plane": plane, "offset": off, "tilts": tilts, "contour": c0,
                      "depth": D, "direction": direction, "angle": ang, "outward": out}}


def _expand_loft(op: dict, where: str) -> list[dict]:
    """Тело по сечениям: сечения в параллельных плоскостях (plane со смещениями offset), соседние соединяются линейчато."""
    from .meshgeom import base_frame, loft_rings, ruled_mesh
    plane = _plane(op, where)
    secs = _get(op, "sections", required=True, where=where)
    if not isinstance(secs, list) or len(secs) < 2:
        raise DSLError(f"{where}: sections — ≥2 сечений {{offset, shape}}")
    items = []
    for i, sc in enumerate(secs):
        w = f"{where}.sections[{i}]"
        if not isinstance(sc, dict):
            raise DSLError(f"{w}: сечение — объект {{offset, shape}}")
        items.append((_num(_get(sc, "offset", required=True, where=w), w + ".offset"),
                      shape_to_contour(_get(sc, "shape", required=True, where=w), w + ".shape")))
    if any(b[0] <= a[0] for a, b in zip(items, items[1:])):
        raise DSLError(f"{where}: offset сечений должны строго возрастать")
    mode = _mode(op, where)
    O, e, f, n = base_frame(plane, 0.0)
    out = []
    for (o0, c0), (o1, c1) in zip(items, items[1:]):
        P0, P1, approx = loft_rings(c0, c1)
        V, F, caps, vol = ruled_mesh(P0, P1, O, e, f, n, o0, o1)
        out.append({"type": "mesh", "mode": mode, "V": V, "F": F, "caps": caps, "vexact": vol, "approx": approx,
                    "build": {"kind": "loft", "plane": plane, "c0": c0, "off0": o0, "c1": c1, "off1": o1}})
    return out


def _expand_sweep(op: dict, where: str) -> list[dict]:
    """Кинематика: труба/пруток круглого сечения d (внутренний d_in) по плоской ломаной path с радиусом гиба bend_r."""
    from .meshgeom import base_frame, fillet_path, path_length, tube_mesh
    plane = _plane(op, where)
    off = _num(op.get("offset", 0), where + ".offset")
    path = _get(op, "path", required=True, where=where)
    if not isinstance(path, list) or len(path) < 2:
        raise DSLError(f"{where}: path — ≥2 точек [u, v] в плоскости {plane}")
    pts = [_pt(q, where + ".path") for q in path]
    d = _num(_get(op, "d", required=True, where=where), where + ".d", gt=0)
    d_in = _num(op.get("d_in", 0) or 0, where + ".d_in", min_=0)
    if d_in >= d:
        raise DSLError(f"{where}: d_in должен быть меньше d")
    R = _num(op.get("bend_r", 1.5 * d), where + ".bend_r", gt=0)
    if len(pts) > 2 and R <= d / 2 + EPS:
        raise DSLError(f"{where}: радиус гиба bend_r={R} должен быть больше d/2={d / 2}")
    du, dv = pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]
    if abs(du) > EPS and abs(dv) > EPS:
        raise DSLError(f"{where}: первый участок траектории должен идти вдоль оси плоскости (u или v)")
    try:
        segs = fillet_path(pts, R)
    except ValueError as e:
        raise DSLError(f"{where}: {e}") from None
    V, F = tube_mesh(segs, base_frame(plane, off), d / 2, d_in / 2)
    vol = math.pi * ((d / 2) ** 2 - (d_in / 2) ** 2) * path_length(segs)  # теорема Паппа — Гульдина
    return [{"type": "mesh", "mode": _mode(op, where), "V": V, "F": F, "vexact": vol, "approx": False,
             "build": {"kind": "sweep", "plane": plane, "offset": off, "segs": segs, "r": d / 2, "ri": d_in / 2,
                       "length": path_length(segs)}}]


def _expand_shell(op: dict, where: str) -> list[dict]:
    """Тонкостенный корпус: тело по контуру shape высотой height со стенкой t; open — какая грань открыта."""
    from .meshgeom import offset_contour
    plane = _plane(op, where)
    z0 = _num(op.get("z0", 0), where + ".z0")
    H = _num(_get(op, "height", required=True, where=where), where + ".height", gt=0)
    t = _num(_get(op, "t", required=True, where=where), where + ".t", gt=0)
    fl = _num(op.get("floor", t), where + ".floor", gt=0)
    opn = str(op.get("open", "top"))
    if opn not in ("top", "bottom", "none", "both"):
        raise DSLError(f"{where}: open — top, bottom, none или both")
    shape = _get(op, "shape", required=True, where=where)
    outer = shape_to_contour(shape, where + ".shape")
    try:
        if isinstance(shape, dict) and shape.get("type") == "rect":  # прямоугольник: внутренний угол острый, если r ≤ t
            W, Hh = _num(shape["w"], where + ".w", gt=0), _num(shape["h"], where + ".h", gt=0)
            if W <= 2 * t or Hh <= 2 * t:
                raise ValueError("стенки толще половины ширины")
            ins = {k: v for k, v in shape.items() if k not in ("x0", "y0")}
            if "x0" in shape or "y0" in shape:
                ins["cx"] = _num(shape.get("x0", 0), where) + W / 2
                ins["cy"] = _num(shape.get("y0", 0), where) + Hh / 2
            ins.update(w=W - 2 * t, h=Hh - 2 * t, r=max(_num(shape.get("r", 0) or 0, where + ".r") - t, 0))
            inner = shape_to_contour(ins, where + ".shape")
        else:
            inner = offset_contour(outer, t)
    except ValueError as e:
        raise DSLError(f"{where}: стенка t={t}: {e}") from None
    a = -t if opn in ("bottom", "both") else fl
    b = H + t if opn in ("top", "both") else H - fl
    if b - a <= EPS:
        raise DSLError(f"{where}: стенки/дно толще высоты")
    return [{"type": "prism", "mode": "add", "plane": plane, "offset": z0, "contours": [outer], "d0": 0.0, "d1": H},
            {"type": "prism", "mode": "cut", "plane": plane, "offset": z0, "contours": [inner], "d0": a, "d1": b}]


def _with_axis(expander):
    """Тела вращения с горизонтальной осью: axis "X" (положение оси — cy, cz) или "Y" (cx, cz);
    координата z в профиле/ступенях — координата вдоль оси."""
    def run(op: dict, where: str) -> list[dict]:
        ax = str(op.get("axis", "Z")).upper()
        if ax not in ("X", "Y", "Z"):
            raise DSLError(f"{where}: axis — X, Y или Z")
        prims = expander(op, where)
        if ax == "Z":
            return prims
        c = {k: _num(op.get(k, 0), f"{where}.{k}") for k in ("cx", "cy", "cz")}
        out = []
        for q in prims:
            if q["type"] != "revolve":
                out.append(q)
                continue
            q = {**q, "axis": ax, "cz": c["cz"]}
            if ax == "X":
                q.update(cx=0.0, cy=c["cy"])
            else:
                q.update(cx=c["cx"], cy=0.0)
            out.append(q)
        return out
    return run


def _hole_points(op: dict, where: str) -> list[tuple[float, float]]:
    if op.get("points") is not None:
        pts = op["points"]
        if not isinstance(pts, list) or not pts:
            raise DSLError(f"{where}: points — непустой список [u, v]")
        return [_pt(p, where + ".points") for p in pts]
    pat = op.get("pattern")
    if not isinstance(pat, dict):
        raise DSLError(f"{where}: задайте points или pattern")
    t = pat.get("type")
    cx, cy = _num(pat.get("cx", 0), where + ".cx"), _num(pat.get("cy", 0), where + ".cy")
    if t == "circular":
        pcd = _num(_get(pat, "pcd", required=True, where=where), where + ".pcd", gt=0)
        n = int(_num(_get(pat, "n", required=True, where=where), where + ".n", gt=0))
        a0 = math.radians(_num(pat.get("start_angle", 0), where + ".start_angle"))
        return [(cx + pcd / 2 * math.cos(a0 + 2 * math.pi * k / n), cy + pcd / 2 * math.sin(a0 + 2 * math.pi * k / n)) for k in range(n)]
    if t == "grid":
        nx = int(_num(pat.get("nx", 1), where + ".nx", gt=0))
        ny = int(_num(pat.get("ny", 1), where + ".ny", gt=0))
        dx = _num(pat.get("dx", 0), where + ".dx")
        dy = _num(pat.get("dy", 0), where + ".dy")
        return [(cx + (i - (nx - 1) / 2) * dx, cy + (j - (ny - 1) / 2) * dy) for i in range(nx) for j in range(ny)]
    if t == "rect":
        W = _num(_get(pat, "w", required=True, where=where), where + ".w", gt=0)
        H = _num(_get(pat, "h", required=True, where=where), where + ".h", gt=0)
        return [(cx + sx * W / 2, cy + sy * H / 2) for sx in (-1, 1) for sy in (-1, 1)]
    raise DSLError(f"{where}: pattern.type должно быть circular, grid или rect")


def _expand_holes(op: dict, where: str) -> list[dict]:
    d = _num(_get(op, "d", required=True, where=where), where + ".d", gt=0)
    plane = _plane(op, where)
    off = _num(op.get("offset", 0), where + ".offset")
    direction = op.get("direction", "-")
    depth = op.get("depth", "through")
    pts = _hole_points(op, where)
    out = []
    if depth == "through":
        d0, d1 = -INF, INF
    else:
        d0, d1 = _range(depth, direction if direction in ("+", "-") else "-", where)
    out.append({"type": "prism", "mode": "cut", "plane": plane, "offset": off,
                "contours": [{"kind": "circle", "c": p, "r": d / 2} for p in pts], "d0": d0, "d1": d1})
    sgn = 1.0 if direction == "+" else -1.0
    if op.get("cbore_d") is not None:
        cd = _num(op["cbore_d"], where + ".cbore_d", gt=d)
        cdep = _num(_get(op, "cbore_depth", required=True, where=where), where + ".cbore_depth", gt=0)
        out.append({"type": "prism", "mode": "cut", "plane": plane, "offset": off,
                    "contours": [{"kind": "circle", "c": p, "r": cd / 2} for p in pts],
                    "d0": min(0, sgn * cdep), "d1": max(0, sgn * cdep)})
    tilts = _tilts(op, where)
    if tilts:
        if op.get("csk_d") is not None:
            raise DSLError(f"{where}: зенковка с наклоном (tilt) не поддерживается")
        return [_tilted(q, tilts) for q in out]
    if op.get("csk_d") is not None:
        if plane != "XY":
            raise DSLError(f"{where}: зенковка (csk_d) поддерживается только для plane='XY'")
        cd = _num(op["csk_d"], where + ".csk_d", gt=d)
        ang = math.radians(_num(op.get("csk_angle", 90), where + ".csk_angle", gt=0))
        dz = (cd - d) / 2 / math.tan(ang / 2)
        for (px, py) in pts:
            prof = [(0.0, off), (cd / 2, off), (d / 2, off + sgn * dz), (0.0, off + sgn * dz)]
            out.append({"type": "revolve", "mode": "cut", "profile": prof, "cx": px, "cy": py})
    return out


def _steps_profile(steps: list, z0: float, where: str) -> list[tuple[float, float]]:
    if not isinstance(steps, list) or not steps:
        raise DSLError(f"{where}: steps — непустой список")
    z = z0
    pts = [(0.0, z0)]
    for i, s in enumerate(steps):
        w = f"{where}[{i}]"
        if not isinstance(s, dict):
            raise DSLError(f"{w}: ступень должна быть объектом")
        Lg = _num(_get(s, "l", required=True, where=w), w + ".l", gt=0)
        if s.get("d") is not None:
            r1 = r2 = _num(s["d"], w + ".d", gt=0) / 2
        else:
            r1 = _num(_get(s, "d1", required=True, where=w), w + ".d1", min_=0) / 2
            r2 = _num(_get(s, "d2", required=True, where=w), w + ".d2", min_=0) / 2
        pts.append((r1, z))
        pts.append((r2, z + Lg))
        z += Lg
    pts.append((0.0, z))
    clean = []
    for p in pts:
        if not clean or math.dist(p, clean[-1]) > EPS:
            clean.append(p)
    if math.dist(clean[0], clean[-1]) < EPS:
        clean.pop()
    return clean


def _expand_shaft(op: dict, where: str) -> list[dict]:
    cx, cy = _num(op.get("cx", 0), where + ".cx"), _num(op.get("cy", 0), where + ".cy")
    z0 = _num(op.get("z0", 0), where + ".z0")
    mode = _mode(op, where)
    outer = _steps_profile(_get(op, "steps", required=True, where=where), z0, where + ".steps")
    total = max(p[1] for p in outer) - z0
    out = [{"type": "revolve", "mode": mode, "profile": outer, "cx": cx, "cy": cy}]
    bore = op.get("bore")
    if op.get("bore_d") is not None:
        bore = [{"d": _num(op["bore_d"], where + ".bore_d", gt=0), "l": total}]
    if bore:
        if op.get("bore_from", "bottom") == "top":
            # растачиваем сверху: переворачиваем список ступеней
            prof = _steps_profile(bore, 0.0, where + ".bore")
            prof = [(r, z0 + total - z) for (r, z) in prof]
        else:
            prof = _steps_profile(bore, z0, where + ".bore")
        out.append({"type": "revolve", "mode": "cut", "profile": prof, "cx": cx, "cy": cy})
    return out


def _expand_revolve(op: dict, where: str) -> list[dict]:
    prof = _get(op, "profile", required=True, where=where)
    if not isinstance(prof, list) or len(prof) < 3:
        raise DSLError(f"{where}: profile — ≥3 точек [r, z]")
    pts = [_pt(p, where + ".profile") for p in prof]
    if any(r < -EPS for r, _ in pts):
        raise DSLError(f"{where}: в profile радиус r не может быть отрицательным")
    pts = [(max(r, 0.0), z) for r, z in pts]
    if math.dist(pts[0], pts[-1]) < EPS:
        pts = pts[:-1]
    if abs(_polygon_area(pts)) < EPS:
        raise DSLError(f"{where}: профиль нулевой площади")
    return [{"type": "revolve", "mode": _mode(op, where), "profile": pts,
             "cx": _num(op.get("cx", 0), where + ".cx"), "cy": _num(op.get("cy", 0), where + ".cy")}]


# ---------------------------------------------------------------- скругления рёбер
_AX = {"X": ("YZ", 0), "Y": ("XZ", 1), "Z": ("XY", 2)}  # ось ребра → плоскость сечения, индекс оси


def _chamfer_loop(pu, pv, su, sv, a, b) -> dict:
    """Треугольник фаски a × b у вершины (pu, pv), квадрант (su, sv)."""
    p0, p1, p2 = (pu, pv), (pu + su * a, pv), (pu, pv + sv * b)
    return {"kind": "loop", "segs": [("L", p0, p1), ("L", p1, p2), ("L", p2, p0)]}


def _corner_loop(pu, pv, su, sv, r) -> dict:
    """Уголок «квадрат r×r минус четверть круга» у вершины (pu, pv), квадрант (su, sv)."""
    a = (pu + su * r, pv)
    b = (pu, pv + sv * r)
    c = (pu + su * r, pv + sv * r)
    return {"kind": "loop", "segs": [("L", (pu, pv), a), ("A", c, r, a, b, su * sv < 0), ("L", b, (pu, pv))]}


def _sign(v, name):
    v = _num(v, name)
    if v == 0:
        raise DSLError(f"{name}: направление должно быть +1 или -1")
    return 1.0 if v > 0 else -1.0


def _expand_round_edge(op: dict, where: str) -> list[dict]:
    """Скругление ребра радиусом r.
    edge: "X" | "Y" | "Z" — прямое ребро, параллельное оси; "circle" — круговое ребро вокруг вертикальной оси.
    at: [u, v] — положение ребра в сечении (для X: [y, z]; для Y: [x, z]; для Z: [x, y]; для circle: [R, z]).
    toward: [su, sv] — куда от ребра лежит скругляемый уголок (±1 по каждой из двух осей сечения):
      выпуклое ребро (kind "convex", по умолчанию) — в сторону материала, материал срезается;
      вогнутое ребро/галтель (kind "concave") — в сторону пустоты, материал добавляется.
    from, to — пределы ребра вдоль его оси (для прямых рёбер); cx, cy — ось кругового ребра."""
    e = str(op.get("edge", "")).upper()
    shape = op.get("shape", "fillet")
    if shape not in ("fillet", "chamfer"):
        raise DSLError(f"{where}: shape — fillet (скругление) или chamfer (фаска)")
    if shape == "chamfer":
        a = _num(_get(op, "c", op.get("r"), required=True, where=where), where + ".c", gt=0)
        b = _num(op.get("c2", a), where + ".c2", gt=0)
        r = max(a, b)
    else:
        r = _num(_get(op, "r", required=True, where=where), where + ".r", gt=0)
    at = _pt(_get(op, "at", required=True, where=where), where + ".at")
    tw = _get(op, "toward", required=True, where=where)
    if not isinstance(tw, (list, tuple)) or len(tw) != 2:
        raise DSLError(f"{where}: toward — [su, sv], каждое +1 или -1")
    su, sv = _sign(tw[0], where + ".toward"), _sign(tw[1], where + ".toward")
    kind = op.get("kind", "convex")
    if kind not in ("convex", "concave"):
        raise DSLError(f"{where}: kind — convex (срезать) или concave (галтель, добавить)")
    mode = "cut" if kind == "convex" else "add"
    if e == "CIRCLE":
        R, z = at
        if R + min(0.0, su) * r < -EPS:
            raise DSLError(f"{where}: скругление заходит за ось (R < r)")
        loop = _chamfer_loop(R, z, su, sv, a, b) if shape == "chamfer" else _corner_loop(R, z, su, sv, r)
        rs = [R, R + su * r]
        zs = [z, z + sv * r]
        box = [(min(rs), min(zs)), (max(rs), min(zs)), (max(rs), max(zs)), (min(rs), max(zs))]
        return [{"type": "revolve", "mode": mode, "profile": box, "ploop": loop,
                 "cx": _num(op.get("cx", 0), where + ".cx"), "cy": _num(op.get("cy", 0), where + ".cy")}]
    if e not in _AX:
        raise DSLError(f"{where}: edge — X, Y, Z или circle")
    plane, _ = _AX[e]
    lo = _num(_get(op, "from", required=True, where=where), where + ".from")
    hi = _num(_get(op, "to", required=True, where=where), where + ".to")
    if hi <= lo:
        raise DSLError(f"{where}: to должно быть больше from")
    loop = _chamfer_loop(at[0], at[1], su, sv, a, b) if shape == "chamfer" else _corner_loop(at[0], at[1], su, sv, r)
    return [{"type": "prism", "mode": mode, "plane": plane, "offset": lo, "contours": [loop], "d0": 0.0, "d1": hi - lo}]


def _expand_plate(op: dict, where: str) -> list[dict]:
    """Пластина w × h × t с углами в плане R и скруглением верхней (и/или нижней) кромки по всему периметру."""
    w = _num(_get(op, "w", required=True, where=where), where + ".w", gt=0)
    h = _num(_get(op, "h", required=True, where=where), where + ".h", gt=0)
    t = _num(_get(op, "t", required=True, where=where), where + ".t", gt=0)
    R = _num(op.get("r", 0) or 0, where + ".r", min_=0)
    ft = _num(op.get("fillet_top", 0) or 0, where + ".fillet_top", min_=0)
    fb = _num(op.get("fillet_bottom", 0) or 0, where + ".fillet_bottom", min_=0)
    cx, cy = _num(op.get("cx", 0), where + ".cx"), _num(op.get("cy", 0), where + ".cy")
    z0 = _num(op.get("z0", 0), where + ".z0")
    f = max(ft, fb)
    if ft + fb > t + EPS:
        raise DSLError(f"{where}: сумма скруглений больше толщины")
    if f > 0 and R < f - EPS:
        raise DSLError(f"{where}: радиус углов в плане r должен быть ≥ радиуса скругления кромки")
    rect = lambda ww, hh, rr: shape_to_contour({"type": "rect", "w": ww, "h": hh, "r": rr, "cx": cx, "cy": cy}, where)  # noqa: E731
    # средний слой — полный контур; верхний/нижний слои — контур, утопленный на радиус скругления
    out = [{"type": "prism", "mode": "add", "plane": "XY", "offset": z0, "contours": [rect(w, h, R)],
            "d0": fb, "d1": t - ft}]
    hx, hy = w / 2 - R, h / 2 - R
    for rf, zc, sz, zl in ((ft, z0 + t - ft, 1.0, (t - ft, t)), (fb, z0 + fb, -1.0, (0.0, fb))):
        if rf <= 0:
            continue
        out.append({"type": "prism", "mode": "add", "plane": "XY", "offset": z0,
                    "contours": [rect(w - 2 * rf, h - 2 * rf, max(R - rf, 0.0))], "d0": zl[0], "d1": zl[1]})

        def qdisk(cu, cv, su):  # четверть круга: центр (cu, cv), наружу su по u, вверх/вниз sz по v
            p1, p2 = (cu + su * rf, cv), (cu, cv + sz * rf)
            return {"kind": "loop", "segs": [("L", (cu, cv), p1), ("A", (cu, cv), rf, p1, p2, su * sz > 0), ("L", p2, (cu, cv))]}
        for s_ in (1.0, -1.0):
            out.append({"type": "prism", "mode": "add", "plane": "YZ", "offset": cx - hx,
                        "contours": [qdisk(cy + s_ * (h / 2 - rf), zc, s_)], "d0": 0.0, "d1": 2 * hx})
            out.append({"type": "prism", "mode": "add", "plane": "XZ", "offset": cy - hy,
                        "contours": [qdisk(cx + s_ * (w / 2 - rf), zc, s_)], "d0": 0.0, "d1": 2 * hy})
        # углы — тело вращения вокруг оси скругления угла: профиль (0..R-rf) + четверть круга
        p1, p2 = (R, zc), (R - rf, zc + sz * rf)
        loop = {"kind": "loop", "segs": [("L", (0.0, zc), p1), ("A", (R - rf, zc), rf, p1, p2, sz > 0),
                                         ("L", p2, (0.0, zc + sz * rf)), ("L", (0.0, zc + sz * rf), (0.0, zc))]}
        zz = sorted([zc, zc + sz * rf])
        for sx in (1.0, -1.0):
            for sy in (1.0, -1.0):
                out.append({"type": "revolve", "mode": "add", "ploop": loop,
                            "profile": [(0.0, zz[0]), (R, zz[0]), (R, zz[1]), (0.0, zz[1])],
                            "cx": cx + sx * hx, "cy": cy + sy * hy})
    return out


def _expand_spring(op: dict, where: str) -> list[dict]:
    """Пружина (винтовая): средний диаметр D, диаметр проволоки d, шаг pitch, число витков n.
    Ось — вертикальная через (cx, cy), начало навивки на высоте z0 (центр сечения), при угле 0 (направление +X).
    Сечение проволоки — круг d в осевой (меридиональной) плоскости."""
    D = _num(_get(op, "D", required=True, where=where), where + ".D", gt=0)
    d = _num(_get(op, "d", required=True, where=where), where + ".d", gt=0)
    p = _num(_get(op, "pitch", required=True, where=where), where + ".pitch", gt=0)
    n = _num(_get(op, "n", required=True, where=where), where + ".n", gt=0)
    if d >= D:
        raise DSLError(f"{where}: диаметр проволоки d должен быть меньше среднего диаметра D")
    if p <= d:
        raise DSLError(f"{where}: шаг pitch должен быть больше диаметра проволоки d (иначе витки пересекаются)")
    if op.get("mode", "add") != "add":
        raise DSLError(f"{where}: пружина может только добавлять материал")
    z0 = _num(op.get("z0", d / 2), where + ".z0")
    return [{"type": "helix", "mode": "add", "R": D / 2, "r": d / 2, "pitch": p, "turns": n,
             "cx": _num(op.get("cx", 0), where + ".cx"), "cy": _num(op.get("cy", 0), where + ".cy"), "z0": z0}]


def _dome_prim(x, y, z, R, mode="add"):
    p1, p2 = (R, z), (0.0, z + R)
    loop = {"kind": "loop", "segs": [("L", (0.0, z), p1), ("A", (0.0, z), R, p1, p2, True), ("L", p2, (0.0, z))]}
    return {"type": "revolve", "mode": mode, "ploop": loop, "profile": [(0.0, z), (R, z), (R, z + R), (0.0, z + R)],
            "cx": x, "cy": y}


def _expand_sphere(op: dict, where: str) -> list[dict]:
    """Шар диаметром d с центром (cx, cy, cz); "mode":"cut" — сферическая полость/лунка."""
    d = _num(_get(op, "d", required=True, where=where), where + ".d", gt=0)
    R = d / 2
    cx, cy, cz = (_num(op.get(k, 0), where + "." + k) for k in ("cx", "cy", "cz"))
    p1, p2 = (0.0, cz - R), (0.0, cz + R)
    loop = {"kind": "loop", "segs": [("L", p2, p1), ("A", (0.0, cz), R, p1, p2, True)]}
    return [{"type": "revolve", "mode": _mode(op, where), "ploop": loop,
             "profile": [(0.0, cz - R), (R, cz - R), (R, cz + R), (0.0, cz + R)], "cx": cx, "cy": cy}]


def _expand_dome(op: dict, where: str) -> list[dict]:
    """Полусферы (купола) на плоскости z: items — таблица строк [x, y, R] (центр основания и радиус)."""
    z = _num(op.get("z", 0), where + ".z")
    items = _get(op, "items", required=True, where=where)
    if not isinstance(items, list) or not items:
        raise DSLError(f"{where}: items — таблица строк [x, y, R]")
    out = []
    for i, it in enumerate(items):
        w = f"{where}.items[{i}]"
        if not isinstance(it, (list, tuple)) or len(it) != 3:
            raise DSLError(f"{w}: строка таблицы должна быть [x, y, R]")
        out.append(_dome_prim(_num(it[0], w), _num(it[1], w), z, _num(it[2], w, gt=0), _mode(op, where)))
    return out


def _plain(p: dict, what: str):
    if p["type"] == "mesh" or p.get("frame") or (p["type"] == "revolve" and p.get("axis", "Z") != "Z"):
        raise DSLError(f"{what}: наклонные элементы, уклон, кинематику, сечения и горизонтальные тела вращения "
                       "размножать нельзя — запишите копии отдельными операциями")


def _translate(p: dict, dx: float, dy: float) -> dict:
    _plain(p, "массив по таблице")
    if p["type"] == "revolve":
        return {**p, "cx": p["cx"] + dx, "cy": p["cy"] + dy}
    if p["type"] != "prism" or p.get("rot"):
        raise DSLError("массив по таблице: допустимы выдавливание, вырез, отверстия, тела вращения (без поворотов)")
    if p["plane"] == "XY":
        du, dv, dw = dx, dy, 0.0
    elif p["plane"] == "XZ":
        du, dv, dw = dx, 0.0, dy
    else:
        du, dv, dw = dy, 0.0, dx
    from_ = p["contours"]
    cs = []
    for c in from_:
        sh = lambda q: (q[0] + du, q[1] + dv)  # noqa: E731
        if c["kind"] == "circle":
            cs.append({"kind": "circle", "c": sh(c["c"]), "r": c["r"]})
        else:
            cs.append({"kind": "loop", "segs": [("L", sh(g[1]), sh(g[2])) if g[0] == "L"
                                                else ("A", sh(g[1]), g[2], sh(g[3]), sh(g[4]), g[5]) for g in c["segs"]]})
    return {**p, "contours": cs, "offset": p["offset"] + dw}


def _mirror_contour(c, flip_u, flip_v, du=0.0, dv=0.0):
    f = lambda q: ((2 * du - q[0]) if flip_u else q[0], (2 * dv - q[1]) if flip_v else q[1])  # noqa: E731
    if c["kind"] == "circle":
        return {"kind": "circle", "c": f(c["c"]), "r": c["r"]}
    odd = flip_u != flip_v
    return {"kind": "loop", "segs": [("L", f(g[1]), f(g[2])) if g[0] == "L"
                                     else ("A", f(g[1]), g[2], f(g[3]), f(g[4]), (not g[5]) if odd else g[5])
                                     for g in c["segs"]]}


def _mirror_prim(p: dict, plane: str, k: float) -> dict:
    """Зеркало относительно плоскости x = k ("YZ") или y = k ("XZ")."""
    _plain(p, "зеркало")
    if p["type"] == "revolve":
        return {**p, "cx": 2 * k - p["cx"]} if plane == "YZ" else {**p, "cy": 2 * k - p["cy"]}
    if p["type"] != "prism" or p.get("rot"):
        raise DSLError("зеркало: допустимы выдавливание, вырез, отверстия, тела вращения (без поворотов)")
    axis = 0 if plane == "YZ" else 1  # отражаемая глобальная ось
    pl = p["plane"]
    if pl == "XY":
        return {**p, "contours": [_mirror_contour(c, axis == 0, axis == 1, k if axis == 0 else 0, k if axis == 1 else 0)
                                  for c in p["contours"]]}
    if (pl == "XZ" and axis == 0) or (pl == "YZ" and axis == 1):  # отражаемая ось — ось u эскиза
        return {**p, "contours": [_mirror_contour(c, True, False, k) for c in p["contours"]]}
    # отражаемая ось — нормаль плоскости
    return {**p, "offset": 2 * k - p["offset"], "d0": -p["d1"], "d1": -p["d0"]}


def _expand_mirror(op: dict, where: str) -> list[dict]:
    """Зеркальная копия вложенных операций относительно плоскости x = k (plane "YZ") или y = k (plane "XZ").
    Исходные операции тоже строятся (keep_original: false — только зеркальная копия)."""
    plane = str(op.get("plane", "YZ")).upper()
    if plane not in ("YZ", "XZ"):
        raise DSLError(f"{where}: plane — YZ (зеркало по x) или XZ (зеркало по y)")
    k = _num(op.get("at", 0), where + ".at")
    inner = op.get("ops")
    if not isinstance(inner, list) or not inner:
        raise DSLError(f"{where}: ops — операции, которые отражаются")
    base = []
    for j, sub in enumerate(inner):
        w = f"{where}.ops[{j}]"
        if not isinstance(sub, dict) or sub.get("op") not in EXPANDERS or sub.get("op") == "spring":
            raise DSLError(f"{w}: недопустимая операция внутри зеркала")
        base.extend(EXPANDERS[sub["op"]](sub, w))
    out = list(base) if op.get("keep_original", True) else []
    return out + [_mirror_prim(p, plane, k) for p in base]


def _expand_table_pattern(op: dict, where: str) -> list[dict]:
    """Массив по таблице: вложенные операции копируются со сдвигами из таблицы points = [[dx, dy], ...]."""
    pts = _get(op, "points", required=True, where=where)
    if not isinstance(pts, list) or not pts:
        raise DSLError(f"{where}: points — таблица сдвигов [[dx, dy], ...]")
    inner = op.get("ops")
    if not isinstance(inner, list) or not inner:
        raise DSLError(f"{where}: ops — операции, которые копируются")
    base = []
    for j, sub in enumerate(inner):
        w = f"{where}.ops[{j}]"
        if not isinstance(sub, dict) or sub.get("op") not in EXPANDERS or sub.get("op") in ("table_pattern", "spring"):
            raise DSLError(f"{w}: недопустимая операция внутри массива")
        base.extend(EXPANDERS[sub["op"]](sub, w))
    return [_translate(p, *_pt(d, where + ".points")) for d in pts for p in base]


METRIC_PITCH = {3: 0.5, 4: 0.7, 5: 0.8, 6: 1.0, 8: 1.25, 10: 1.5, 12: 1.75, 14: 2.0, 16: 2.0, 18: 2.5,
                20: 2.5, 22: 2.5, 24: 3.0, 27: 3.0, 30: 3.5, 36: 4.0, 42: 4.5, 48: 5.0}


def _expand_thread(op: dict, where: str) -> list[dict]:
    """Условное изображение метрической резьбы на цилиндре (наружная) или в отверстии (внутренняя).
    Геометрию не меняет: стержень/отверстие строится гладким номинальным диаметром отдельной операцией."""
    d = _num(_get(op, "d", required=True, where=where), where + ".d", gt=0)
    pitch = op.get("pitch")
    pitch = _num(pitch, where + ".pitch", gt=0) if pitch is not None else METRIC_PITCH.get(int(round(d)), round(d * 0.15, 2))
    z0 = _num(_get(op, "z_from", required=True, where=where), where + ".z_from")
    z1 = _num(_get(op, "z_to", required=True, where=where), where + ".z_to")
    if z1 <= z0:
        raise DSLError(f"{where}: z_to должно быть больше z_from")
    return [{"type": "thread", "d": d, "pitch": pitch, "internal": bool(op.get("internal", False)),
             "cx": _num(op.get("cx", 0), where + ".cx"), "cy": _num(op.get("cy", 0), where + ".cy"),
             "z0": z0, "z1": z1}]


def _expand_torus(op: dict, where: str) -> list[dict]:
    """Тор (кольцо круглого сечения) с осью, параллельной Z, через (cx, cy).
    R — радиус средней окружности (от оси до центра сечения), r — радиус сечения; z — высота центра сечения.
    Можно задать D (наружный диаметр) и d (диаметр сечения) вместо R и r."""
    if op.get("R") is not None:
        R = _num(op["R"], where + ".R", gt=0)
    elif op.get("D") is not None:
        R = None
    else:
        raise DSLError(f"{where}: задайте R (радиус средней окружности) или D (наружный диаметр)")
    if op.get("r") is not None:
        r = _num(op["r"], where + ".r", gt=0)
    elif op.get("d") is not None:
        r = _num(op["d"], where + ".d", gt=0) / 2
    else:
        raise DSLError(f"{where}: задайте r (радиус сечения) или d (диаметр сечения)")
    if R is None:
        R = _num(op["D"], where + ".D", gt=0) / 2 - r
    if r > R - EPS:
        raise DSLError(f"{where}: радиус сечения r={r} должен быть меньше R={R} (иначе тор без отверстия)")
    z = _num(op.get("z", r), where + ".z")
    square = [(R - r, z - r), (R + r, z - r), (R + r, z + r), (R - r, z + r)]  # для габарита и сетки
    return [{"type": "revolve", "mode": _mode(op, where), "profile": square, "pcircle": (R, z, r),
             "cx": _num(op.get("cx", 0), where + ".cx"), "cy": _num(op.get("cy", 0), where + ".cy")}]


def _expand_edge(op: dict, where: str) -> list[dict]:
    kind = op["op"]
    key = "r" if kind == "fillet" else "d"
    raw = op.get(key, op.get("size"))
    if raw is None:
        raise DSLError(f"{where}: нет размера '{key}'")
    size = _num(raw, f"{where}.{key}", gt=0)
    e = op.get("edges")
    if not isinstance(e, dict):
        raise DSLError(f"{where}: edges — объект-селектор, например {{\"type\":\"circle\",\"z\":30}}")
    if e.get("type", "any") not in ("line", "circle", "any"):
        raise DSLError(f"{where}: edges.type — line, circle или any")
    return [{"type": "edge", "kind": kind, "size": size, "edges": dict(e)}]


def _rot_pt(pt, ang, cx, cy):
    c, s_ = math.cos(ang), math.sin(ang)
    x, y = pt[0] - cx, pt[1] - cy
    return (cx + c * x - s_ * y, cy + s_ * x + c * y)


def _rot_contour(c: dict, ang, cx, cy) -> dict:
    if c["kind"] == "circle":
        return {"kind": "circle", "c": _rot_pt(c["c"], ang, cx, cy), "r": c["r"]}
    segs = []
    for sg in c["segs"]:
        if sg[0] == "L":
            segs.append(("L", _rot_pt(sg[1], ang, cx, cy), _rot_pt(sg[2], ang, cx, cy)))
        else:
            segs.append(("A", _rot_pt(sg[1], ang, cx, cy), sg[2], _rot_pt(sg[3], ang, cx, cy),
                         _rot_pt(sg[4], ang, cx, cy), sg[5]))
    return {"kind": "loop", "segs": segs}


def rotate_prim(p: dict, deg: float, cx: float = 0.0, cy: float = 0.0) -> dict:
    """Поворот примитива вокруг оси, параллельной Z, через (cx, cy)."""
    if abs(deg % 360) < 1e-9:
        return p
    ang = math.radians(deg)
    q = dict(p)
    _plain(p, "круговой массив")
    if p["type"] == "revolve":
        q["cx"], q["cy"] = _rot_pt((p["cx"], p["cy"]), ang, cx, cy)
    elif p["type"] == "prism" and p["plane"] == "XY":
        q["contours"] = [_rot_contour(c, ang, cx, cy) for c in p["contours"]]
    elif p["type"] == "prism":  # вертикальная плоскость XZ/YZ → плоскость под углом
        old = p.get("rot") or {"angle": 0.0, "cx": cx, "cy": cy}
        if abs(old["cx"] - cx) > 1e-9 or abs(old["cy"] - cy) > 1e-9:
            raise DSLError("вложенные повороты вокруг разных центров не поддерживаются")
        q["rot"] = {"angle": old["angle"] + deg, "cx": cx, "cy": cy}
    else:
        raise DSLError("скругления/фаски нельзя размножать круговым массивом")
    return q


def _expand_pattern(op: dict, where: str) -> list[dict]:
    """Круговой массив: операции ops повторяются с поворотом вокруг оси Z (через cx, cy)."""
    inner = op.get("ops")
    if not isinstance(inner, list) or not inner:
        raise DSLError(f"{where}: ops — непустой список операций для размножения")
    cx, cy = _num(op.get("cx", 0), where + ".cx"), _num(op.get("cy", 0), where + ".cy")
    if op.get("angles") is not None:
        if not isinstance(op["angles"], list) or not op["angles"]:
            raise DSLError(f"{where}: angles — список углов поворота в градусах")
        angles = [_num(a, where + ".angles") for a in op["angles"]]
    else:
        n = int(_num(_get(op, "n", required=True, where=where), where + ".n", gt=0))
        step = _num(op.get("step", 360 / n), where + ".step")
        angles = [k * step for k in range(n)]
    base = []
    for j, sub in enumerate(inner):
        w = f"{where}.ops[{j}]"
        if not isinstance(sub, dict) or sub.get("op") not in EXPANDERS or sub.get("op") == "circular_pattern":
            raise DSLError(f"{w}: внутри массива допустимы extrude, cut, holes, shaft, revolve")
        base.extend(EXPANDERS[sub["op"]](sub, w))
    return [rotate_prim(p, a, cx, cy) for a in angles for p in base]


EXPANDERS = {
    "extrude": _expand_extrude,
    "cut": lambda op, w: _expand_extrude({**op, "mode": "cut"}, w),
    "holes": _expand_holes,
    "shaft": _with_axis(_expand_shaft),
    "revolve": _with_axis(_expand_revolve),
    "loft": _expand_loft,
    "sweep": _expand_sweep,
    "shell": _expand_shell,
    "fillet": _expand_edge,
    "chamfer": _expand_edge,
    "circular_pattern": _expand_pattern,
    "torus": _with_axis(_expand_torus),
    "thread": _expand_thread,
    "dome": _expand_dome,
    "sphere": _expand_sphere,
    "mirror": _expand_mirror,
    "table_pattern": _expand_table_pattern,
    "spring": _expand_spring,
    "round_edge": _expand_round_edge,
    "plate": _expand_plate,
}


def normalize(spec: Any) -> list[dict]:
    """JSON от LLM → список примитивов. Бросает DSLError с понятным текстом."""
    if isinstance(spec, list):
        spec = {"ops": spec}
    if not isinstance(spec, dict):
        raise DSLError("ответ должен быть JSON-объектом с полем 'ops'")
    ops = spec.get("ops")
    if not isinstance(ops, list) or not ops:
        raise DSLError("нет непустого списка 'ops'")
    prims: list[dict] = []
    for i, op in enumerate(ops):
        where = f"ops[{i}]"
        if not isinstance(op, dict):
            raise DSLError(f"{where}: операция должна быть объектом")
        name = op.get("op")
        if name not in EXPANDERS:
            raise DSLError(f"{where}: неизвестная операция {name!r}; есть {sorted(EXPANDERS)}")
        prims.extend(EXPANDERS[name](op, where))
    if any(p["type"] == "helix" for p in prims) and len(prims) > 1:
        raise DSLError("пружина (spring) строится отдельной деталью — без других операций")
    if prims[0]["type"] in ("edge", "thread") or prims[0]["mode"] != "add":
        raise DSLError("первая операция должна добавлять материал (mode='add')")
    return prims


def has_edge_ops(prims: list[dict]) -> bool:
    return any(p["type"] == "edge" for p in prims)
