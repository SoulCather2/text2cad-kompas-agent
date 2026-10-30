"""
Геометрия «сложных» форм для независимого расчёта: наклонные плоскости, кинематика (труба по траектории),
тело по сечениям, выдавливание с уклоном.

Наклонная плоскость считается точно (как обычная призма, только в произвольной системе координат).
Кинематика, тело по сечениям и уклон представляются замкнутой треугольной сеткой; лучи пересекаются с сеткой
точно, погрешность — только от разбиения кривых (окружность — 1,5° на сегмент, площадь сечения
трубы подгоняется к точной), обычно < 0,05 % по объёму.
"""
from __future__ import annotations

import math

import numpy as np

# ---------------------------------------------------------------- системы координат
BASE_FRAMES = {  # (e, f, n): оси эскиза u, v и нормаль w — как у обычных плоскостей
    "XY": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "XZ": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
    "YZ": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
}
AXIS_VEC = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}


def _rodrigues(v, k, ang):
    v, k = np.asarray(v, float), np.asarray(k, float)
    c, s = math.cos(ang), math.sin(ang)
    return v * c + np.cross(k, v) * s + k * np.dot(k, v) * (1 - c)


def base_frame(plane: str, offset: float):
    e, f, n = (np.array(x) for x in BASE_FRAMES[plane])
    return n * offset, e, f, n


def apply_tilts(O, e, f, tilts):
    """Повороты плоскости вместе с эскизом вокруг осей ‖ X/Y/Z через точку point (правило правой руки)."""
    O, e, f = np.asarray(O, float), np.asarray(e, float), np.asarray(f, float)
    for t in tilts:
        k = np.array(AXIS_VEC[t["axis"]])
        a = math.radians(t["angle"])
        P = np.asarray(t["point"], float)
        O = P + _rodrigues(O - P, k, a)
        e, f = _rodrigues(e, k, a), _rodrigues(f, k, a)
    n = np.cross(e, f)
    return O, e, f, n


def frame_dict(O, e, f, n):
    return {"O": tuple(map(float, O)), "e": tuple(map(float, e)), "f": tuple(map(float, f)), "n": tuple(map(float, n))}


# ---------------------------------------------------------------- 2D-контуры
ARC_STEP = math.radians(1.5)


def _arc_angles(seg):
    _, cen, r, p1, p2, ccw = seg
    a1 = math.atan2(p1[1] - cen[1], p1[0] - cen[0])
    a2 = math.atan2(p2[1] - cen[1], p2[0] - cen[0])
    full = math.dist(p1, p2) < 1e-12
    if ccw:
        span = (a2 - a1) % (2 * math.pi) or (2 * math.pi if full else 0.0)
    else:
        span = -((a1 - a2) % (2 * math.pi) or (2 * math.pi if full else 0.0))
    return a1, span


def seg_counts(c) -> list[int]:
    """Число участков разбиения для каждого сегмента контура."""
    if c["kind"] == "circle":
        return [240]
    out = []
    for sg in c["segs"]:
        if sg[0] == "L":
            out.append(1)
        else:
            _, span = _arc_angles(sg)
            out.append(max(2, int(math.ceil(abs(span) / ARC_STEP))))
    return out


def sample_contour(c, counts=None) -> np.ndarray:
    """Точки контура (без повтора последней) с заданным разбиением сегментов."""
    counts = counts or seg_counts(c)
    if c["kind"] == "circle":
        (u, v), r = c["c"], c["r"]
        k = counts[0]
        a = np.arange(k) * 2 * math.pi / k
        return np.stack([u + r * np.cos(a), v + r * np.sin(a)], axis=1)
    pts = []
    for sg, k in zip(c["segs"], counts):
        if sg[0] == "L":
            p1, p2 = np.array(sg[1]), np.array(sg[2])
            for i in range(k):
                pts.append(p1 + (p2 - p1) * i / k)
        else:
            cen, r = sg[1], sg[2]
            a1, span = _arc_angles(sg)
            for i in range(k):
                a = a1 + span * i / k
                pts.append((cen[0] + r * math.cos(a), cen[1] + r * math.sin(a)))
    return np.array(pts, float)


def signed_area(P: np.ndarray) -> float:
    x, y = P[:, 0], P[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))


def offset_contour(c, delta: float):
    """Эквидистанта контура: delta > 0 — внутрь. Прямые — со стыками «на ус», дуги — изменением радиуса.
    Возвращает новый контур той же структуры (сегмент в сегмент)."""
    if c["kind"] == "circle":
        r = c["r"] - delta
        if r <= 1e-6:
            raise ValueError("эквидистанта: окружность вырождается")
        return {"kind": "circle", "c": c["c"], "r": r}
    sgn = 1.0 if signed_area(sample_contour(c)) > 0 else -1.0
    d = delta * sgn  # смещение влево по ходу обхода (внутрь для обхода против часовой)
    segs = c["segs"]
    off = []
    for sg in segs:
        if sg[0] == "L":
            p1, p2 = np.array(sg[1], float), np.array(sg[2], float)
            t = (p2 - p1) / np.linalg.norm(p2 - p1)
            nl = np.array([-t[1], t[0]])
            off.append(["L", p1 + d * nl, p2 + d * nl])
        else:
            _, cen, r, p1, p2, ccw = sg
            r2 = r - d if ccw else r + d  # центр слева у дуги против часовой
            if r2 <= 1e-6:
                raise ValueError("эквидистанта: дуга вырождается (радиус скругления меньше смещения)")
            cen_ = np.array(cen, float)
            q1 = cen_ + (np.array(p1) - cen_) * r2 / r
            q2 = cen_ + (np.array(p2) - cen_) * r2 / r
            off.append(["A", tuple(cen), r2, q1, q2, ccw])
    n = len(off)
    for i in range(n):
        a, b = off[i], off[(i + 1) % n]
        ea = a[2] if a[0] == "L" else a[4]
        sb = b[1] if b[0] == "L" else b[3]
        if np.linalg.norm(ea - sb) < 1e-7:
            continue
        if a[0] == "L" and b[0] == "L":  # стык «на ус»
            p, r_ = a[1], a[2] - a[1]
            q, s_ = b[1], b[2] - b[1]
            den = r_[0] * s_[1] - r_[1] * s_[0]
            if abs(den) < 1e-12:
                raise ValueError("эквидистанта: параллельные стыкующиеся отрезки")
            tt = ((q[0] - p[0]) * s_[1] - (q[1] - p[1]) * s_[0]) / den
            X = p + tt * r_
            a[2] = X
            b[1] = X
        else:
            raise ValueError("эквидистанта: стык дуги без касания не поддерживается")
    segs2 = []
    for s in off:
        if s[0] == "L":
            segs2.append(("L", tuple(map(float, s[1])), tuple(map(float, s[2]))))
        else:
            segs2.append(("A", s[1], float(s[2]), tuple(map(float, s[3])), tuple(map(float, s[4])), s[5]))
    out = {"kind": "loop", "segs": segs2}
    P = sample_contour(out)
    if signed_area(P) * sgn <= 1e-9:
        raise ValueError("эквидистанта: контур вывернулся (смещение слишком большое)")
    return out


# ---------------------------------------------------------------- триангуляция
def _ear_clip(P: np.ndarray) -> list[tuple[int, int, int]]:
    n = len(P)
    idx = list(range(n))
    if signed_area(P) < 0:
        idx.reverse()
    cross = lambda a, b, c: (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])  # noqa: E731
    # выпуклый многоугольник — веером
    conv = all(cross(P[idx[i - 1]], P[idx[i]], P[idx[(i + 1) % n]]) >= -1e-12 for i in range(n))
    if conv:
        return [(idx[0], idx[i], idx[i + 1]) for i in range(1, n - 1)]
    tris = []
    guard = 0
    while len(idx) > 3 and guard < 100000:
        guard += 1
        m = len(idx)
        cut = False
        for i in range(m):
            a, b, c = idx[i - 1], idx[i], idx[(i + 1) % m]
            if cross(P[a], P[b], P[c]) <= 1e-14:
                continue
            ok = True
            for j in idx:
                if j in (a, b, c):
                    continue
                p = P[j]
                if (cross(P[a], P[b], p) >= 0 and cross(P[b], P[c], p) >= 0 and cross(P[c], P[a], p) >= 0):
                    ok = False
                    break
            if ok:
                tris.append((a, b, c))
                idx.pop(i)
                cut = True
                break
        if not cut:
            raise ValueError("не удалось разбить сечение на треугольники (самопересечение?)")
    tris.append(tuple(idx))
    return tris


def ruled_mesh(P0: np.ndarray, P1: np.ndarray, O, e, f, n, w0: float, w1: float):
    """Линейчатое тело между двумя кольцами (одинаковое число точек, точка в точку) в плоскостях w0 и w1.
    Возвращает (V, F боковой поверхности, торцы caps, точный объём).
    Торцы — плоские многоугольники: лучи проверяются с ними точно (точка в многоугольнике), без треугольников."""
    k = len(P0)
    O, e, f, n = (np.asarray(x, float) for x in (O, e, f, n))
    to3 = lambda P, w: O + P[:, :1] * e + P[:, 1:2] * f + w * n  # noqa: E731
    # промежуточные кольца на той же линейчатой поверхности: длинные косые треугольники
    # дают огромные габаритные прямоугольники и замедляют пересечение с лучами
    span = float(np.max(np.linalg.norm(to3(P1, w1) - to3(P0, w0), axis=1)))
    m = int(min(24, max(1, math.ceil(span / 3.0))))
    V = np.concatenate([to3(P0 + (P1 - P0) * (j / m), w0 + (w1 - w0) * j / m) for j in range(m + 1)])
    F = []
    for b in range(m):
        for i in range(k):
            j = (i + 1) % k
            a0, a1 = b * k, (b + 1) * k
            F += [(a0 + i, a0 + j, a1 + j), (a0 + i, a1 + j, a1 + i)]
    fr = (tuple(O), tuple(e), tuple(f), tuple(n))
    caps = [{"P": np.asarray(P0, float), "w": float(w0), "frame": fr},
            {"P": np.asarray(P1, float), "w": float(w1), "frame": fr}]
    cr = lambda a, b: a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]  # noqa: E731
    A0 = 0.5 * cr(P0, np.roll(P0, -1, 0)).sum()
    A1 = 0.5 * cr(P1, np.roll(P1, -1, 0)).sum()
    M = 0.25 * (cr(P0, np.roll(P1, -1, 0)) + cr(P1, np.roll(P0, -1, 0))).sum()
    vol = abs(w1 - w0) * abs(A0 + A1 + M) / 3.0
    return V, np.array(F, dtype=np.int64), caps, vol


def point_in_poly(P: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Чётность пересечений луча +u с многоугольником P (без больших промежуточных массивов)."""
    ins = np.zeros(len(u), bool)
    Q = np.roll(P, -1, 0)
    for (x1, y1), (x2, y2) in zip(P, Q):
        if y1 == y2:
            continue
        c = ((y1 <= v) & (v < y2)) | ((y2 <= v) & (v < y1))
        xi = x1 + (v - y1) * (x2 - x1) / (y2 - y1)
        ins ^= c & (u < xi)
    return ins


def cap_hits(cap: dict, A: int, coord: dict) -> np.ndarray:
    """Пересечения лучей вдоль оси A с плоским торцом: столбец t (inf — мимо)."""
    R = len(next(iter(coord.values())))
    O, e, f, n = (np.array(x) for x in cap["frame"])
    if abs(n[A]) < 1e-12:
        return np.full(R, np.inf)
    P0 = [np.zeros(R) if i == A else coord[i] for i in range(3)]
    D = [P0[i] - O[i] for i in range(3)]
    ww = D[0] * n[0] + D[1] * n[1] + D[2] * n[2]
    t = (cap["w"] - ww) / n[A]
    X = [D[i] + (t if i == A else 0.0) for i in range(3)]
    u = X[0] * e[0] + X[1] * e[1] + X[2] * e[2]
    v = X[0] * f[0] + X[1] * f[1] + X[2] * f[2]
    return np.where(point_in_poly(cap["P"], u, v), t, np.inf)


# ---------------------------------------------------------------- тело по сечениям
def _resample(P: np.ndarray, s: np.ndarray) -> np.ndarray:
    Q = np.concatenate([P, P[:1]])
    L = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(Q, axis=0), axis=1))])
    t = s * L[-1]
    return np.stack([np.interp(t, L, Q[:, 0]), np.interp(t, L, Q[:, 1])], axis=1)


def _params(P: np.ndarray) -> np.ndarray:
    Q = np.concatenate([P, P[:1]])
    L = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(Q, axis=0), axis=1))])
    return L[:-1] / L[-1]


def _ccw_from_angle0(P: np.ndarray) -> np.ndarray:
    if signed_area(P) < 0:
        P = P[::-1]
    c = P.mean(axis=0)
    ang = np.arctan2(P[:, 1] - c[1], P[:, 0] - c[0])
    i0 = int(np.argmin(np.abs(ang)))
    return np.roll(P, -i0, axis=0)


def loft_rings(c0, c1):
    """Соответствие точек двух сечений: одинаковое число отрезков — вершина в вершину,
    иначе — по доле длины контура от точки на луче +u из центра (против часовой)."""
    poly = lambda c: c["kind"] == "loop" and all(s[0] == "L" for s in c["segs"])  # noqa: E731
    if poly(c0) and poly(c1) and len(c0["segs"]) == len(c1["segs"]):
        P0, P1 = sample_contour(c0), sample_contour(c1)
        if signed_area(P0) < 0:
            P0 = P0[::-1]
        if signed_area(P1) < 0:
            P1 = P1[::-1]
        # вершина в вершину, начиная с ближайших по углу
        c0m, c1m = P0.mean(axis=0), P1.mean(axis=0)
        a0 = np.arctan2(P0[:, 1] - c0m[1], P0[:, 0] - c0m[0])
        a1 = np.arctan2(P1[:, 1] - c1m[1], P1[:, 0] - c1m[0])
        k = len(P0)
        best = min(range(k), key=lambda s: sum(abs(math.remainder(a0[i] - a1[(i + s) % k], 2 * math.pi)) for i in range(k)))
        return P0, np.roll(P1, -best, axis=0), False
    # сначала уплотняем (у прямоугольника иначе только 4 вершины), затем начинаем с точки на луче +u из центра
    dense = lambda P: _resample(P, np.linspace(0, 1, 720, endpoint=False))  # noqa: E731
    P0 = _ccw_from_angle0(dense(sample_contour(c0)))
    P1 = _ccw_from_angle0(dense(sample_contour(c1)))
    s = np.unique(np.concatenate([_params(P0), _params(P1)]))
    return _resample(P0, s), _resample(P1, s), True


# ---------------------------------------------------------------- труба по траектории
def fillet_path(pts: list, R: float):
    """Плоская ломаная с радиусом гиба R → сегменты ('L', p1, p2) / ('A', c, R, p1, p2, ccw)."""
    P = [np.array(p, float) for p in pts]
    if len(P) < 2:
        raise ValueError("траектория: нужно ≥ 2 точек")
    segs = []
    cur = P[0]
    tl_prev = 0.0
    for i in range(1, len(P) - 1):
        d1 = P[i] - P[i - 1]
        d2 = P[i + 1] - P[i]
        l1, l2 = np.linalg.norm(d1), np.linalg.norm(d2)
        if l1 < 1e-9 or l2 < 1e-9:
            raise ValueError("траектория: повторяющиеся точки")
        d1, d2 = d1 / l1, d2 / l2
        cr = d1[0] * d2[1] - d1[1] * d2[0]
        th = math.acos(max(-1.0, min(1.0, float(np.dot(d1, d2)))))
        if th < 1e-9:
            continue
        if th > math.pi - 1e-6:
            raise ValueError("траектория: разворот на 180° в одной точке")
        tl = R * math.tan(th / 2)
        if tl + tl_prev > l1 + 1e-9:
            raise ValueError(f"траектория: участок {i} слишком короткий для радиуса гиба {R}")
        a = P[i] - d1 * tl
        b = P[i] + d2 * tl
        nl = np.array([-d1[1], d1[0]]) * (1 if cr > 0 else -1)
        cen = a + nl * R
        if np.linalg.norm(a - cur) > 1e-9:
            segs.append(("L", tuple(cur), tuple(a)))
        segs.append(("A", tuple(cen), R, tuple(a), tuple(b), cr > 0))
        cur = b
        tl_prev = tl
    last = P[-1]
    if np.linalg.norm(last - P[-2]) < tl_prev - 1e-9:
        raise ValueError("траектория: последний участок короче гиба")
    if np.linalg.norm(last - cur) > 1e-9:
        segs.append(("L", tuple(cur), tuple(last)))
    return segs


def path_length(segs) -> float:
    L = 0.0
    for s in segs:
        if s[0] == "L":
            L += math.dist(s[1], s[2])
        else:
            _, span = _arc_angles(s)
            L += abs(span) * s[2]
    return L


def tube_mesh(segs, frame, r_out: float, r_in: float = 0.0, k: int = 120):
    """Сетка трубы (или прутка при r_in = 0) вдоль плоской траектории. frame = (O, e, f, n) плоскости траектории."""
    O, e, f, n = (np.asarray(x, float) for x in frame)
    # точки и касательные средней линии (2D)
    C, T = [], []
    for i, s in enumerate(segs):
        if s[0] == "L":
            p1, p2 = np.array(s[1]), np.array(s[2])
            t = (p2 - p1) / np.linalg.norm(p2 - p1)
            if i == 0:
                C.append(p1)
                T.append(t)
            C.append(p2)
            T.append(t)
        else:
            cen, r = np.array(s[1]), s[2]
            a1, span = _arc_angles(s)
            m = max(2, int(math.ceil(abs(span) / math.radians(3.0))))
            for j in range(0 if i == 0 else 1, m + 1):
                a = a1 + span * j / m
                p = cen + r * np.array([math.cos(a), math.sin(a)])
                tt = np.array([-math.sin(a), math.cos(a)]) * (1 if span > 0 else -1)
                C.append(p)
                T.append(tt)
    C, T = np.array(C), np.array(T)
    # площадь вписанного многоугольника = площади круга
    fix = math.sqrt(2 * math.pi / (k * math.sin(2 * math.pi / k)))
    phi = np.arange(k) * 2 * math.pi / k
    rings_o, rings_i = [], []
    for p, t in zip(C, T):
        c3 = O + p[0] * e + p[1] * f
        t3 = t[0] * e + t[1] * f
        b3 = np.cross(t3, n)
        dirs = np.cos(phi)[:, None] * n + np.sin(phi)[:, None] * b3
        rings_o.append(c3 + r_out * fix * dirs)
        if r_in > 0:
            rings_i.append(c3 + r_in * fix * dirs)
    m = len(C)
    V = [np.concatenate(rings_o)]
    F = []

    def side(base, rev):
        for a in range(m - 1):
            for i in range(k):
                j = (i + 1) % k
                q = [base + a * k + i, base + a * k + j, base + (a + 1) * k + j, base + (a + 1) * k + i]
                F.extend([(q[0], q[1], q[2]), (q[0], q[2], q[3])] if not rev else [(q[0], q[2], q[1]), (q[0], q[3], q[2])])

    side(0, False)
    if r_in > 0:
        V.append(np.concatenate(rings_i))
        side(m * k, True)
        for a in (0, m - 1):  # торцы-кольца
            for i in range(k):
                j = (i + 1) % k
                oi, oj, ii, ij = a * k + i, a * k + j, m * k + a * k + i, m * k + a * k + j
                F.extend([(oi, ii, ij), (oi, ij, oj)])
    else:
        Vc = [C[0], C[-1]]
        nc = m * k
        V.append(np.array([O + q[0] * e + q[1] * f for q in Vc]))
        for i in range(k):
            j = (i + 1) % k
            F.append((nc, j, i))
            F.append((nc + 1, (m - 1) * k + i, (m - 1) * k + j))
    return np.concatenate(V), np.array(F, dtype=np.int64)


# ---------------------------------------------------------------- лучи × сетка
def mesh_toggles_grid(V: np.ndarray, F: np.ndarray, A: int, gb: np.ndarray, gc: np.ndarray) -> np.ndarray:
    """Пересечения лучей вдоль оси A (сетка gb × gc по двум другим осям, порядок meshgrid ij) с замкнутой сеткой.
    Возвращает (len(gb)·len(gc), K) — отсортированные координаты пересечений вдоль A, дополнено +inf."""
    B, C = [i for i in range(3) if i != A]
    R = len(gb) * len(gc)
    ob, oc = np.argsort(gb, kind="stable"), np.argsort(gc, kind="stable")
    sb, sc = gb[ob], gc[oc]
    P = V[F]
    pb, pc, pa = P[:, :, B].copy(), P[:, :, C].copy(), P[:, :, A].copy()
    ar = (pb[:, 1] - pb[:, 0]) * (pc[:, 2] - pc[:, 0]) - (pb[:, 2] - pb[:, 0]) * (pc[:, 1] - pc[:, 0])
    keep = np.abs(ar) > 1e-14
    pb, pc, pa, ar = pb[keep], pc[keep], pa[keep], ar[keep]
    neg = ar < 0
    for X in (pb, pc, pa):
        X[neg, 1], X[neg, 2] = X[neg, 2].copy(), X[neg, 1].copy()
    ar = np.abs(ar)
    ib0 = np.searchsorted(sb, pb.min(axis=1), "left")
    ib1 = np.searchsorted(sb, pb.max(axis=1), "right")
    ic0 = np.searchsorted(sc, pc.min(axis=1), "left")
    ic1 = np.searchsorted(sc, pc.max(axis=1), "right")
    nb, nc = np.maximum(ib1 - ib0, 0), np.maximum(ic1 - ic0, 0)
    cnt = nb * nc
    rays_all, ts_all = [], []
    tri_ids = np.nonzero(cnt)[0]
    # пакетами, чтобы не раздувать память
    start = 0
    while start < len(tri_ids):
        acc, end = 0, start
        while end < len(tri_ids) and (acc < 4_000_000 or end == start):
            acc += cnt[tri_ids[end]]
            end += 1
        ids = tri_ids[start:end]
        start = end
        c_ = cnt[ids]
        tri = np.repeat(ids, c_)
        off = np.repeat(np.cumsum(c_) - c_, c_)
        loc = np.arange(c_.sum()) - off
        bi = ib0[tri] + loc // nc[tri]
        ci = ic0[tri] + loc % nc[tri]
        x, y = sb[bi], sc[ci]
        ax_, ay_, aa = pb[tri], pc[tri], pa[tri]
        inside = np.ones(len(tri), bool)
        lam = []
        for k0, k1 in ((1, 2), (2, 0), (0, 1)):  # λ для вершины, противоположной ребру k0→k1
            dx, dy = ax_[:, k1] - ax_[:, k0], ay_[:, k1] - ay_[:, k0]
            w = dx * (y - ay_[:, k0]) - dy * (x - ax_[:, k0])
            tl = (dy > 0) | ((dy == 0) & (dx < 0))
            inside &= (w > 0) | ((w == 0) & tl)
            lam.append(w)
        if not inside.any():
            continue
        t = (lam[0] * aa[:, 0] + lam[1] * aa[:, 1] + lam[2] * aa[:, 2]) / ar[tri]
        rays_all.append((ob[bi] * len(gc) + oc[ci])[inside])
        ts_all.append(t[inside])
    if not rays_all:
        return np.full((R, 2), np.inf)
    ray = np.concatenate(rays_all)
    ts = np.concatenate(ts_all)
    o = np.lexsort((ts, ray))
    ray, ts = ray[o], ts[o]
    counts = np.bincount(ray, minlength=R)
    K = max(2, int(counts.max()))
    first = np.cumsum(counts) - counts
    rank = np.arange(len(ray)) - first[ray]
    T = np.full((R, K), np.inf)
    T[ray, rank] = ts
    return T
