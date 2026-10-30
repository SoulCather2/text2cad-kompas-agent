"""
Независимый расчёт объёма и габарита по примитивам DSL (только NumPy).

Метод: пучки лучей вдоль X, Y и Z. Для каждого луча точно (аналитически) считаются
интервалы, где луч внутри примитива, затем над интервалами выполняются булевы
операции (объединение/вычитание). Объём = сумма длин интервалов × площадь ячейки.
Вдоль луча расчёт точный, погрешность только от шага сетки (обычно < 0,1 %).

Скругления/фаски рёбер (примитив "edge") здесь НЕ моделируются: результат
помечается approx=True и проверяется только по КОМПАСу.
"""
from __future__ import annotations

import math

import numpy as np

BIG = 1e6
AXES = {"XY": (0, 1, 2), "XZ": (0, 2, 1), "YZ": (1, 2, 0)}  # (u, v, w) → индексы X/Y/Z
JIT = 0.01234567


# ---------------------------------------------------------------- пересечения
def _arc_contains(ang, a_start, a_end, ccw, full=False):
    if full:
        return np.ones_like(ang, dtype=bool)
    if not ccw:
        a_start, a_end = a_end, a_start
    span = (a_end - a_start) % (2 * math.pi)
    return ((ang - a_start) % (2 * math.pi)) <= span + 1e-12


def crossings(contours: list[dict], k: int, F: np.ndarray) -> np.ndarray:
    """Пересечения прямых {координата k = F} с контурами.
    Возвращает отсортированные значения второй координаты, (R, M), дополнено +inf."""
    cols = []
    o = 1 - k
    for c in contours:
        if c["kind"] == "circle":
            dk = F - c["c"][k]
            s2 = c["r"] ** 2 - dk * dk
            ok = s2 > 0
            s = np.sqrt(np.where(ok, s2, 0))
            cols.append(np.where(ok, c["c"][o] - s, np.inf))
            cols.append(np.where(ok, c["c"][o] + s, np.inf))
            continue
        for seg in c["segs"]:
            if seg[0] == "L":
                p1, p2 = seg[1], seg[2]
                a1, a2, b1, b2 = p1[k], p2[k], p1[o], p2[o]
                if a1 == a2:
                    continue
                cond = ((a1 <= F) & (F < a2)) | ((a2 <= F) & (F < a1))
                t = (F - a1) / (a2 - a1)
                cols.append(np.where(cond, b1 + t * (b2 - b1), np.inf))
            else:
                _, cen, r, p1, p2, ccw = seg
                full = math.dist(p1, p2) < 1e-12
                a_s = math.atan2(p1[1] - cen[1], p1[0] - cen[0])
                a_e = math.atan2(p2[1] - cen[1], p2[0] - cen[0])
                dk = F - cen[k]
                s2 = r * r - dk * dk
                ok = s2 > 0
                s = np.sqrt(np.where(ok, s2, 0))
                for sg in (-1.0, 1.0):
                    ov = cen[o] + sg * s
                    pt = [None, None]
                    pt[k], pt[o] = F, ov
                    ang = np.arctan2(pt[1] - cen[1], pt[0] - cen[0])
                    good = ok & _arc_contains(ang, a_s, a_e, ccw, full)
                    cols.append(np.where(good, ov, np.inf))
    if not cols:
        return np.full((len(F), 2), np.inf)
    M = np.sort(np.stack(cols, axis=1), axis=1)
    keep = max(2, int(np.isfinite(M).sum(axis=1).max(initial=0)))
    return M[:, :keep]


def inside2d(contours, U, V) -> np.ndarray:
    cr = crossings(contours, 1, V)  # вдоль u при фиксированном v
    return ((cr < U[:, None]).sum(axis=1) % 2) == 1


# ---------------------------------------------------------------- примитивы
BASE_EN = {"XZ": ((1.0, 0.0), (0.0, 1.0)), "YZ": ((0.0, 1.0), (1.0, 0.0))}  # (e, n) в плоскости XY


def vframe(p):
    """Повёрнутая вертикальная плоскость: начало O, ось эскиза e (горизонт.), нормаль n (горизонт.)."""
    r = p["rot"]
    a = math.radians(r["angle"])
    c, s_ = math.cos(a), math.sin(a)
    R = lambda v: (c * v[0] - s_ * v[1], s_ * v[0] + c * v[1])  # noqa: E731
    e0, n0 = BASE_EN[p["plane"]]
    e, n = R(e0), R(n0)
    rc = R((r["cx"], r["cy"]))
    O = (r["cx"] - rc[0], r["cy"] - rc[1])
    return O, e, n


def _vprism_toggles(p, A, coord):
    R_ = len(next(iter(coord.values())))
    O, e, n = vframe(p)
    w0 = max(-BIG, p["offset"] + p["d0"])
    w1 = min(BIG, p["offset"] + p["d1"])
    C = p["contours"]
    if A == 2:
        dx, dy = coord[0] - O[0], coord[1] - O[1]
        u = dx * e[0] + dy * e[1]
        w = dx * n[0] + dy * n[1]
        T = crossings(C, 0, u)
        T[~((w >= w0) & (w <= w1))] = np.inf
        return T
    # луч вдоль X (A=0) или Y (A=1): p(t) = t по оси A
    other = 1 - A
    z = coord[2]
    q = coord[other] - O[other]
    ea, eo, na, no = e[A], e[other], n[A], n[other]
    u0 = q * eo - O[A] * ea          # u(t) = ea·t + u0
    ww0 = q * no - O[A] * na         # w(t) = na·t + ww0
    if abs(ea) > 1e-9:
        U = crossings(C, 1, z)
        T = np.where(np.isfinite(U), (U - u0[:, None]) / ea, np.inf)
        T = np.sort(T, axis=1)
    else:
        ins = inside2d(C, u0, z)
        T = _empty(R_)
        T[ins, 0], T[ins, 1] = -BIG, BIG
    if abs(na) > 1e-9:
        ta, tb = (w0 - ww0) / na, (w1 - ww0) / na
        slab = np.stack([np.minimum(ta, tb), np.maximum(ta, tb)], axis=1)
        return combine(T, slab, "and")
    T = T.copy()
    T[~((ww0 >= w0) & (ww0 <= w1))] = np.inf
    return T


def _empty(R):
    return np.full((R, 2), np.inf)


def _rot2(c: dict, ca: float, sa: float) -> dict:
    """Поворот контура на угол (cos, sin) вокруг начала координат эскиза."""
    R_ = lambda q: (ca * q[0] - sa * q[1], sa * q[0] + ca * q[1])  # noqa: E731
    if c["kind"] == "circle":
        return {"kind": "circle", "c": R_(c["c"]), "r": c["r"]}
    return {"kind": "loop", "segs": [("L", R_(g[1]), R_(g[2])) if g[0] == "L"
                                     else ("A", R_(g[1]), g[2], R_(g[3]), R_(g[4]), g[5]) for g in c["segs"]]}


def _fprism_toggles(p, A, coord):
    """Призма на плоскости произвольного положения: frame = (O, e, f, n), тело при w ∈ [d0, d1] вдоль n."""
    R_ = len(next(iter(coord.values())))
    fr = p["frame"]
    O, e, f, n = (np.array(fr[k]) for k in ("O", "e", "f", "n"))
    w0, w1 = max(-BIG, p["d0"]), min(BIG, p["d1"])
    # луч: точка с координатой A = 0 плюс t вдоль оси A
    P0 = [np.zeros(R_) if i == A else coord[i] for i in range(3)]
    D = [P0[i] - O[i] for i in range(3)]
    u0 = D[0] * e[0] + D[1] * e[1] + D[2] * e[2]
    v0 = D[0] * f[0] + D[1] * f[1] + D[2] * f[2]
    ww = D[0] * n[0] + D[1] * n[1] + D[2] * n[2]
    eA, fA, nA = e[A], f[A], n[A]
    s_ = math.hypot(eA, fA)
    if s_ > 1e-9:
        ca, sa = eA / s_, fA / s_                  # направление луча в плоскости эскиза
        C2 = [_rot2(c, ca, -sa) for c in p["contours"]]  # поворот так, чтобы направление стало +u'
        uu = ca * u0 + sa * v0
        vv = -sa * u0 + ca * v0
        U = crossings(C2, 1, vv)
        T = np.where(np.isfinite(U), (U - uu[:, None]) / s_, np.inf)
        T = np.sort(T, axis=1)
    else:
        ins = inside2d(p["contours"], u0, v0)
        T = _empty(R_)
        T[ins, 0], T[ins, 1] = -BIG, BIG
    if abs(nA) > 1e-9:
        ta, tb = (w0 - ww) / nA, (w1 - ww) / nA
        slab = np.stack([np.minimum(ta, tb), np.maximum(ta, tb)], axis=1)
        return combine(T, slab, "and")
    T = T.copy()
    T[~((ww >= w0) & (ww <= w1))] = np.inf
    return T


REV_AXES = {"Z": (2, 0, 1), "X": (0, 1, 2), "Y": (1, 0, 2)}  # ось вращения, две другие оси


def rev_center(p) -> list[float]:
    return [p.get("cx", 0.0), p.get("cy", 0.0), p.get("cz", 0.0)]


def prim_toggles(p: dict, A: int, coord: dict[int, np.ndarray], cache: dict | None = None) -> np.ndarray:
    R = len(next(iter(coord.values())))
    if p["type"] == "mesh":
        T = cache[id(p)]
        if p.get("caps"):
            from .meshgeom import cap_hits
            cols = [cap_hits(c, A, coord)[:, None] for c in p["caps"]]
            T = np.sort(np.concatenate([T] + cols, axis=1), axis=1)
            keep = max(2, int(np.isfinite(T).sum(axis=1).max(initial=0)))
            T = T[:, :keep]
        return T
    if p["type"] == "prism" and p.get("frame"):
        return _fprism_toggles(p, A, coord)
    if p["type"] == "prism" and p.get("rot"):
        return _vprism_toggles(p, A, coord)
    if p["type"] == "prism":
        ua, va, wa = AXES[p["plane"]]
        w0 = max(-BIG, p["offset"] + p["d0"])
        w1 = min(BIG, p["offset"] + p["d1"])
        if A == wa:
            ins = inside2d(p["contours"], coord[ua], coord[va])
            T = _empty(R)
            T[ins, 0], T[ins, 1] = w0, w1
            return T
        W = coord[wa]
        inw = (W >= w0) & (W <= w1)
        if A == ua:
            T = crossings(p["contours"], 1, coord[va])
        else:
            T = crossings(p["contours"], 0, coord[ua])
        T[~inw] = np.inf
        return T
    if p["type"] == "revolve":
        if p.get("ploop"):
            prof = p["ploop"]
        elif p.get("pcircle"):
            R, zc, rr = p["pcircle"]
            prof = {"kind": "circle", "c": (R, zc), "r": rr}
        else:
            prof = {"kind": "loop", "segs": [("L", p["profile"][i], p["profile"][(i + 1) % len(p["profile"])])
                                             for i in range(len(p["profile"]))]}
        ax, i1, i2 = REV_AXES[p.get("axis", "Z")]
        cen = rev_center(p)
        if A == ax:
            r = np.hypot(coord[i1] - cen[i1], coord[i2] - cen[i2])
            return crossings([prof], 0, r)
        Rt = crossings([prof], 1, coord[ax])  # переключения по радиусу
        o = i2 if A == i1 else i1
        d, c0 = coord[o] - cen[o], cen[A]
        s2 = Rt * Rt - (d * d)[:, None]
        ok = np.isfinite(Rt) & (s2 > 0)
        s = np.sqrt(np.where(ok, s2, 0))
        T = np.concatenate([np.where(ok, c0 - s, np.inf), np.where(ok, c0 + s, np.inf)], axis=1)
        T = np.sort(T, axis=1)
        keep = max(2, int(np.isfinite(T).sum(axis=1).max(initial=0)))
        return T[:, :keep]
    raise ValueError(p["type"])


# ---------------------------------------------------------------- булевы операции над интервалами
def _inside(T, m):
    return ((T[:, None, :] < m[:, :, None]).sum(axis=2) % 2) == 1


def combine(T1, T2, op: str) -> np.ndarray:
    E = np.sort(np.concatenate([T1, T2], axis=1), axis=1)
    m = np.concatenate([(E[:, :-1] + E[:, 1:]) / 2, np.full((len(E), 1), np.inf)], axis=1)
    a, b = _inside(T1, m), _inside(T2, m)
    st = (a | b) if op == "add" else (a & b) if op == "and" else (a & ~b)
    prev = np.concatenate([np.zeros((len(E), 1), bool), st[:, :-1]], axis=1)
    ch = (st != prev) & np.isfinite(E)
    T = np.sort(np.where(ch, E, np.inf), axis=1)
    keep = max(2, int(np.isfinite(T).sum(axis=1).max(initial=0)))
    return T[:, :keep]


def sweep(prims, A: int, coord: dict[int, np.ndarray], chunk: int = 30000, mesh_cache: dict | None = None) -> np.ndarray:
    R = len(next(iter(coord.values())))
    outs = []
    for s in range(0, R, chunk):
        cc = {k: v[s:s + chunk] for k, v in coord.items()}
        cache = {k: v[s:s + chunk] for k, v in (mesh_cache or {}).items()}
        T = None
        for p in prims:
            if p["type"] in ("edge", "thread"):
                continue
            P = prim_toggles(p, A, cc, cache)
            T = P if T is None else combine(T, P, p["mode"])
        outs.append(T)
    K = max(o.shape[1] for o in outs)
    return np.concatenate([np.pad(o, ((0, 0), (0, K - o.shape[1])), constant_values=np.inf) for o in outs])


# ---------------------------------------------------------------- габариты примитивов
def _contour_bbox(c):
    if c["kind"] == "circle":
        (u, v), r = c["c"], c["r"]
        return u - r, u + r, v - r, v + r
    us, vs = [], []
    for seg in c["segs"]:
        if seg[0] == "L":
            us += [seg[1][0], seg[2][0]]
            vs += [seg[1][1], seg[2][1]]
        else:
            (u, v), r = seg[1], seg[2]
            us += [u - r, u + r]
            vs += [v - r, v + r]
    return min(us), max(us), min(vs), max(vs)


def _vprism_corners(p):
    O, e, n = vframe(p)
    bb = np.array([_contour_bbox(c) for c in p["contours"]])
    us = (bb[:, 0].min(), bb[:, 1].max())
    vs = (bb[:, 2].min(), bb[:, 3].max())
    ws = (max(-BIG, p["offset"] + p["d0"]), min(BIG, p["offset"] + p["d1"]))
    return [(O[0] + u * e[0] + w * n[0], O[1] + u * e[1] + w * n[1], v) for u in us for v in vs for w in ws]


def _contour_pts(c):
    """Характерные точки контура: вершины, а у дуг/окружностей — ещё крайние точки по осям эскиза."""
    if c["kind"] == "circle":
        (u, v), r = c["c"], c["r"]
        return [(u - r, v), (u + r, v), (u, v - r), (u, v + r)]
    pts = []
    for g in c["segs"]:
        if g[0] == "L":
            pts += [g[1], g[2]]
        else:
            (u, v), r = g[1], g[2]
            pts += [g[3], g[4], (u - r, v), (u + r, v), (u, v - r), (u, v + r)]
    return pts


def _fprism_corners(p):
    fr = p["frame"]
    O, e, f, n = (np.array(fr[k]) for k in ("O", "e", "f", "n"))
    ws = [w for w in (p["d0"], p["d1"]) if math.isfinite(w)]
    out = []
    for c in p["contours"]:
        for (u, v) in _contour_pts(c):
            for w in ws:
                out.append(O + u * e + v * f + w * n)
    return out


def _fprism_extent(p):
    """Точный габарит призмы на наклонной плоскости (окружности — по эллипсу)."""
    fr = p["frame"]
    O, e, f, n = (np.array(fr[k]) for k in ("O", "e", "f", "n"))
    lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
    for w in (p["d0"], p["d1"]):
        for c in p["contours"]:
            if c["kind"] == "circle":
                (u, v), r = c["c"], c["r"]
                C = O + u * e + v * f + w * n
                ext = r * np.sqrt(e ** 2 + f ** 2)
                lo, hi = np.minimum(lo, C - ext), np.maximum(hi, C + ext)
            else:
                from .meshgeom import sample_contour
                P = sample_contour(c)
                Q = O + P[:, :1] * e + P[:, 1:2] * f + w * n
                lo, hi = np.minimum(lo, Q.min(axis=0)), np.maximum(hi, Q.max(axis=0))
    return lo, hi


def rev_extent(p):
    ax, i1, i2 = REV_AXES[p.get("axis", "Z")]
    cen = rev_center(p)
    rmax = max(r for r, _ in p["profile"])
    zs = [z for _, z in p["profile"]]
    lo, hi = np.zeros(3), np.zeros(3)
    lo[ax], hi[ax] = min(zs), max(zs)
    for i in (i1, i2):
        lo[i], hi[i] = cen[i] - rmax, cen[i] + rmax
    return lo, hi


def prims_bbox(prims) -> np.ndarray:
    lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
    for p in prims:
        if p["type"] == "mesh" and p["mode"] == "add":
            lo, hi = np.minimum(lo, p["V"].min(axis=0)), np.maximum(hi, p["V"].max(axis=0))
            continue
        if p["type"] == "prism" and p["mode"] == "add" and p.get("frame"):
            a, b = _fprism_extent(p)
            lo, hi = np.minimum(lo, a), np.maximum(hi, b)
            continue
        if p["type"] == "prism" and p["mode"] == "add" and p.get("rot"):
            pts = np.array(_vprism_corners(p))
            lo, hi = np.minimum(lo, pts.min(axis=0)), np.maximum(hi, pts.max(axis=0))
            continue
        if p["type"] == "prism" and p["mode"] == "add":
            ua, va, wa = AXES[p["plane"]]
            bb = np.array([_contour_bbox(c) for c in p["contours"]])
            lo[ua], hi[ua] = min(lo[ua], bb[:, 0].min()), max(hi[ua], bb[:, 1].max())
            lo[va], hi[va] = min(lo[va], bb[:, 2].min()), max(hi[va], bb[:, 3].max())
            lo[wa] = min(lo[wa], p["offset"] + p["d0"])
            hi[wa] = max(hi[wa], p["offset"] + p["d1"])
        elif p["type"] == "revolve" and p["mode"] == "add":
            a, b = rev_extent(p)
            lo, hi = np.minimum(lo, a), np.maximum(hi, b)
    return np.stack([lo, hi])


def breakpoints(prims) -> list[list[float]]:
    """Координаты граней/вершин по осям X, Y, Z — к ним привязываются границы ячеек сетки,
    чтобы плоские грани не давали ошибку дискретизации."""
    br = [[], [], []]
    for p in prims:
        if p["type"] == "mesh":
            V = p["V"]
            pts = V if len(V) <= 200 else np.concatenate([V[V.argmin(axis=0)], V[V.argmax(axis=0)]])
            if p.get("caps"):  # плоскости торцов — по ним выравнивается сетка
                for c in p["caps"]:
                    O, e, f, nn = (np.array(x) for x in c["frame"])
                    q = O + c["w"] * nn
                    for i in range(3):
                        if abs(abs(nn[i]) - 1) < 1e-12:
                            br[i].append(float(q[i]))
            for i in range(3):
                br[i] += [float(x) for x in pts[:, i]]
            continue
        if p["type"] == "prism" and p.get("frame"):
            for q in _fprism_corners(p):
                for i in range(3):
                    if abs(q[i]) < BIG / 2:
                        br[i].append(float(q[i]))
            continue
        if p["type"] == "prism" and p.get("rot"):
            for x, y, z in _vprism_corners(p):
                if abs(x) < BIG / 2 and abs(y) < BIG / 2:
                    br[0].append(x)
                    br[1].append(y)
                br[2].append(z)
            continue
        if p["type"] == "prism":
            ua, va, wa = AXES[p["plane"]]
            for c in p["contours"]:
                if c["kind"] == "circle":
                    (u, v), r = c["c"], c["r"]
                    br[ua] += [u - r, u, u + r]
                    br[va] += [v - r, v, v + r]
                    continue
                for seg in c["segs"]:
                    if seg[0] == "L":
                        br[ua] += [seg[1][0], seg[2][0]]
                        br[va] += [seg[1][1], seg[2][1]]
                    else:
                        (u, v), r = seg[1], seg[2]
                        br[ua] += [seg[3][0], seg[4][0], u]
                        br[va] += [seg[3][1], seg[4][1], v]
            for w in (p["offset"] + p["d0"], p["offset"] + p["d1"]):
                if math.isfinite(w):
                    br[wa].append(w)
        elif p["type"] == "revolve":
            ax, i1, i2 = REV_AXES[p.get("axis", "Z")]
            cen = rev_center(p)
            for r, z in p["profile"]:
                br[i1] += [cen[i1] - r, cen[i1] + r]
                br[i2] += [cen[i2] - r, cen[i2] + r]
                br[ax].append(z)
    return br


def grid1d(lo: float, hi: float, brk: list[float], h: float):
    pts = sorted({lo, hi, *[b for b in brk if lo < b < hi]})
    edges = [pts[0]]
    for a, b in zip(pts[:-1], pts[1:]):
        if b - a < 1e-9:
            continue
        k = max(1, int(math.ceil((b - a) / h)))
        edges += list(np.linspace(a, b, k + 1)[1:])
    e = np.array(edges)
    return (e[:-1] + e[1:]) / 2, np.diff(e)


# ---------------------------------------------------------------- измерение
def helix_measure(h: dict) -> dict:
    """Точный объём и габарит винтовой пружины с сечением-кругом в осевой плоскости:
    объём = площадь сечения × путь центра сечения по окружности (винтовое движение не меняет объём)."""
    R, r, p, n = h["R"], h["r"], h["pitch"], h["turns"]
    vol = math.pi * r * r * 2 * math.pi * R * n
    ang = 2 * math.pi * n
    if n >= 1:
        dx = dy = 2 * (R + r)
    else:  # неполный виток: габарит по дуге
        th = np.linspace(0, ang, 2001)
        xs, ys = R * np.cos(th), R * np.sin(th)
        dx, dy = xs.max() - xs.min() + 2 * r, ys.max() - ys.min() + 2 * r
    dz = n * p + 2 * r
    bb = [float(dx), float(dy), float(dz)]
    return {"volume": float(vol), "bbox": bb, "bbox_sorted": sorted(bb), "approx": False, "spread": 0.0}


def measure(prims, n: int = 400, _mesh_fix: bool = True) -> dict:
    """Возвращает {'volume': мм³, 'bbox': [dx, dy, dz], 'bbox_sorted': [...],
    'approx': bool, 'spread': расхождение объёма между тремя направлениями}."""
    if prims and prims[0]["type"] == "helix":
        return helix_measure(prims[0])
    box = prims_bbox(prims)
    if not np.all(np.isfinite(box)):
        raise ValueError("не удалось определить габарит (нет добавляющих операций?)")
    span = box[1] - box[0]
    pad = 0.02 * span.max() + 0.5
    lo, hi = box[0] - pad, box[1] + pad
    vols, ext = [], np.zeros((3, 2))
    brk = breakpoints(prims)
    for A in (2, 0, 1):
        B, C = [i for i in range(3) if i != A]
        h = max(hi[B] - lo[B], hi[C] - lo[C]) / n
        gb, wb = grid1d(lo[B], hi[B], brk[B], h)
        gc, wc = grid1d(lo[C], hi[C], brk[C], h)
        # доп. лучи (с нулевым весом) точно по вершинам — для точного габарита
        xb = np.array(sorted({b for b in brk[B] if lo[B] < b < hi[B]}))
        xc = np.array(sorted({c for c in brk[C] if lo[C] < c < hi[C]}))
        gb, wb = np.concatenate([gb, xb]), np.concatenate([wb, np.zeros(len(xb))])
        gc, wc = np.concatenate([gc, xc]), np.concatenate([wc, np.zeros(len(xc))])
        gb = gb + JIT * 1e-4 * h  # уводим лучи с точных вершин
        gc = gc + JIT * 1.7e-4 * h
        GB, GC = np.meshgrid(gb, gc, indexing="ij")
        WA = np.outer(wb, wc).ravel()
        coord = {B: GB.ravel(), C: GC.ravel()}
        mesh_cache = {}
        if any(p["type"] == "mesh" for p in prims):
            from .meshgeom import mesh_toggles_grid
            for p in prims:
                if p["type"] == "mesh":
                    mesh_cache[id(p)] = mesh_toggles_grid(p["V"], p["F"], A, gb, gc)
        T = sweep(prims, A, coord, mesh_cache=mesh_cache)
        fin = np.isfinite(T)
        L = np.where(fin[:, 1::2], T[:, 1::2], 0) - np.where(fin[:, 0::2], T[:, 0::2], 0)
        vols.append(float((L.sum(axis=1) * WA).sum()))
        if fin.any():
            ext[A] = [T[fin].min(), T[fin].max()]
    v = float(np.mean(vols))
    if _mesh_fix:
        # поправка на шаг сетки лучей у криволинейных сеток: точный объём сетки − объём той же сетки лучами
        for p in prims:
            if p["type"] == "mesh" and p.get("vexact"):
                alone = measure([{**p, "mode": "add"}], n, _mesh_fix=False)["volume"]
                v += (1 if p["mode"] == "add" else -1) * (p["vexact"] - alone)
    bb = [float(ext[i, 1] - ext[i, 0]) for i in range(3)]
    return {
        "volume": v,
        "bbox": bb,
        "bbox_sorted": sorted(bb),
        "approx": any(p["type"] == "edge" or p.get("approx") for p in prims),
        "spread": float((max(vols) - min(vols)) / v) if v > 0 else 0.0,
    }


def same_result(a: dict, b: dict, vol_tol: float = 0.01, bbox_tol: float = 0.2) -> bool:
    if a["volume"] <= 0 or b["volume"] <= 0:
        return False
    if abs(a["volume"] - b["volume"]) / max(a["volume"], b["volume"]) > vol_tol:
        return False
    return all(abs(x - y) <= bbox_tol for x, y in zip(a["bbox_sorted"], b["bbox_sorted"]))
