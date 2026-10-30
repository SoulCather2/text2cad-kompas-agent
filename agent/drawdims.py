"""
План чертежа по примитивам детали: раскладка видов и список размеров (без КОМПАСа).

Соответствие осей видов КОМПАСа (проверено diag): координаты вида (u, v) в мм модели
  «Спереди»: (Y, Z)    «Сверху»: (Y, −X)    «Слева»: (X, Z)
Отверстия с плоскости XY видны кружками на виде сверху, YZ — спереди, XZ — слева.
"""
from __future__ import annotations

import math
from collections import defaultdict

from .refgeom import prims_bbox

VIEW_OF_PLANE = {"XY": "top", "YZ": "front", "XZ": "left"}


def to_view(view: str, x: float, y: float, z: float) -> tuple[float, float]:
    if view == "front":
        return (y, z)
    if view == "top":
        return (y, -x)
    return (x, z)


def plane_to_global(plane: str, u: float, v: float, w: float = 0.0):
    if plane == "XY":
        return (u, v, w)
    if plane == "XZ":
        return (u, w, v)
    return (w, u, v)


def view_box(view, lo, hi):
    """Прямоугольник детали в координатах вида: (umin, umax, vmin, vmax)."""
    corners = [to_view(view, x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
    us, vs = [c[0] for c in corners], [c[1] for c in corners]
    return min(us), max(us), min(vs), max(vs)


def fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def plan(prims: list[dict]) -> dict:
    if prims and prims[0]["type"] == "helix":
        h = prims[0]
        lo = [h["cx"] - h["R"] - h["r"], h["cy"] - h["R"] - h["r"], h["z0"] - h["r"]]
        hi = [h["cx"] + h["R"] + h["r"], h["cy"] + h["R"] + h["r"], h["z0"] + h["r"] + h["turns"] * h["pitch"]]
        return {"lo": lo, "hi": hi, "boxes": {v: view_box(v, lo, hi) for v in ("front", "top", "left")}, "dims": {}}
    lo, hi = prims_bbox(prims)
    boxes = {v: view_box(v, lo, hi) for v in ("front", "top", "left")}
    dims = defaultdict(list)  # view -> list of dicts

    # габариты: высота и ширина на виде спереди, длина (X) на виде сверху
    u0, u1, v0, v1 = boxes["front"]
    dims["front"].append({"kind": "lin", "p1": (u0, v0), "p2": (u1, v0), "side": "below", "level": 0})
    dims["front"].append({"kind": "lin", "p1": (u0, v0), "p2": (u0, v1), "side": "left", "level": 0})
    u0, u1, v0, v1 = boxes["top"]
    dims["top"].append({"kind": "lin", "p1": (u0, v0), "p2": (u0, v1), "side": "left", "level": 0})

    # отверстия: группируем по виду и диаметру
    holes = defaultdict(list)  # (view, d) -> [(u, v)]
    pockets = []
    for p in prims:
        if p["type"] != "prism" or p.get("rot") or p.get("frame"):
            continue
        circles = [c for c in p["contours"] if c["kind"] == "circle"]
        if p["mode"] == "add":
            circles = circles[1:] if p["contours"][0]["kind"] == "circle" else circles
        view = VIEW_OF_PLANE[p["plane"]]
        for c in circles:
            g = plane_to_global(p["plane"], c["c"][0], c["c"][1], p["offset"])
            holes[(view, round(2 * c["r"], 3))].append(to_view(view, *g))
        if p["mode"] == "cut":
            for c in p["contours"]:
                if c["kind"] == "loop":
                    pts = [s[1] for s in c["segs"]] + [s[2] for s in c["segs"] if s[0] == "L"]
                    us, vs = [q[0] for q in pts], [q[1] for q in pts]
                    a = plane_to_global(p["plane"], min(us), min(vs), p["offset"])
                    b = plane_to_global(p["plane"], max(us), max(vs), p["offset"])
                    pockets.append((view, to_view(view, *a), to_view(view, *b)))

    for (view, d), pts in holes.items():
        uniq = sorted(set((round(u, 3), round(v, 3)) for u, v in pts))
        n = len(uniq)
        cu, cv = uniq[0]
        dims[view].append({"kind": "diam", "c": (cu, cv), "r": d / 2,
                           "prefix": (f"{n} отв. " if n > 1 else "")})
        # координаты центров от левой и верхней кромок вида
        bu0, bu1, bv0, bv1 = boxes[view]
        us = sorted(set(u for u, _ in uniq if abs(u - bu0) > 1e-6))[:4]
        vs = sorted(set(v for _, v in uniq if abs(v - bv1) > 1e-6), reverse=True)[:4]
        for u in us:
            dims[view].append({"kind": "lin", "p1": (bu0, bv1), "p2": (u, bv1), "side": "above", "level": None})
        for v in vs:
            dims[view].append({"kind": "lin", "p1": (bu1, bv1), "p2": (bu1, v), "side": "right", "level": None})

    # размеры карманов/пазов (только величина, без привязки)
    for view, a, b in pockets[:6]:
        (ua, va), (ub, vb) = a, b
        u_lo, u_hi, v_lo, v_hi = min(ua, ub), max(ua, ub), min(va, vb), max(va, vb)
        dims[view].append({"kind": "lin", "p1": (u_lo, v_hi), "p2": (u_hi, v_hi), "side": "inside", "level": None})
        dims[view].append({"kind": "lin", "p1": (u_hi, v_lo), "p2": (u_hi, v_hi), "side": "inside_v", "level": None})

    # тела вращения: диаметры ступеней на виде спереди
    for p in prims:
        if p["type"] != "revolve" or p.get("pcircle") or p.get("ploop") or p.get("axis", "Z") != "Z":
            continue
        prof = p["profile"]
        seen = set()
        for i in range(len(prof)):
            (r1, z1), (r2, z2) = prof[i], prof[(i + 1) % len(prof)]
            if abs(r1 - r2) < 1e-6 and r1 > 1e-6 and abs(z2 - z1) > 1e-6 and round(r1, 3) not in seen:
                seen.add(round(r1, 3))
                zm = (z1 + z2) / 2
                dims["front"].append({"kind": "lin", "p1": (p["cy"] - r1, zm), "p2": (p["cy"] + r1, zm),
                                      "side": "inside", "level": None, "diameter": True})

    # уровни выносных линий, чтобы размеры не накладывались
    for view, lst in dims.items():
        lvl = defaultdict(int)
        for d in lst:
            if d["kind"] != "lin":
                continue
            if d["level"] is None:
                lvl[d["side"]] += 1
                d["level"] = lvl[d["side"]]
            else:
                lvl[d["side"]] = max(lvl[d["side"]], d["level"])
    return {"lo": list(lo), "hi": list(hi), "boxes": boxes, "dims": dict(dims)}


# ---------------------------------------------------------------- размеры по назначению (ГОСТ 2.109-73)
CATS = ("габаритный", "установочный", "присоединительный", "эксплуатационный")


def _concyclic(pts, tol=1e-3):
    if len(pts) < 3:
        return None
    cu = sum(u for u, _ in pts) / len(pts)
    cv = sum(v for _, v in pts) / len(pts)
    rs = [math.hypot(u - cu, v - cv) for u, v in pts]
    if rs[0] > tol and max(rs) - min(rs) < tol:
        return (cu, cv), rs[0]
    return None


def candidates(prims: list[dict]) -> dict:
    """Все размеры, которые МОЖНО поставить, с описанием назначения — для выбора по ГОСТ 2.109-73.
    Возвращает {"lo","hi","boxes","items":[{"id","view","dims":[...],"desc","hint"}]}."""
    if prims and prims[0]["type"] == "helix":
        P = plan(prims)
        h = prims[0]
        u0, u1, v0, v1 = P["boxes"]["front"]
        items = [{"id": "G1", "view": "front", "hint": "габаритный",
                  "desc": f"наружный диаметр {fmt(2 * (h['R'] + h['r']))} и высота пружины",
                  "dims": [{"kind": "lin", "p1": (u0, v0), "p2": (u1, v0), "side": "below", "level": 0},
                           {"kind": "lin", "p1": (u0, v0), "p2": (u0, v1), "side": "left", "level": 0}]}]
        return {"lo": P["lo"], "hi": P["hi"], "boxes": P["boxes"], "items": items}
    lo, hi = prims_bbox(prims)
    boxes = {v: view_box(v, lo, hi) for v in ("front", "top", "left")}
    items = []
    u0, u1, v0, v1 = boxes["front"]
    items.append({"id": "G1", "view": "front", "hint": "габаритный", "desc": f"габарит по Y (ширина) = {fmt(u1 - u0)}",
                  "dims": [{"kind": "lin", "p1": (u0, v0), "p2": (u1, v0), "side": "below", "level": 0}]})
    lu0, lu1, lv0, lv1 = boxes["left"]
    items.append({"id": "G2", "view": "front", "hint": "габаритный", "desc": f"габарит по Z (высота) = {fmt(v1 - v0)}",
                  "dims": [{"kind": "lin", "p1": (u0, v0), "p2": (u0, v1), "side": "left", "level": 0}],
                  # тот же размер на виде слева — если на нём больше нечего поставить
                  "alt": {"view": "left", "dims": [{"kind": "lin", "p1": (lu0, lv0), "p2": (lu0, lv1), "side": "left", "level": 0}]}})
    u0, u1, v0, v1 = boxes["top"]
    items.append({"id": "G3", "view": "top", "hint": "габаритный", "desc": f"габарит по X (длина) = {fmt(v1 - v0)}",
                  "dims": [{"kind": "lin", "p1": (u0, v0), "p2": (u0, v1), "side": "left", "level": 0}]})

    # центры круглых элементов (диски, ступицы, тела вращения) — для «окружности центров» отверстий
    round_centers = defaultdict(list)
    for p in prims:
        if p["type"] == "revolve" and p.get("axis", "Z") == "Z":
            round_centers["top"].append(to_view("top", p["cx"], p["cy"], 0))
        elif p["type"] == "prism" and not p.get("rot") and not p.get("frame"):
            for c in p["contours"]:
                if c["kind"] == "circle" and p["mode"] == "add":
                    view = VIEW_OF_PLANE[p["plane"]]
                    round_centers[view].append(to_view(view, *plane_to_global(p["plane"], c["c"][0], c["c"][1], p["offset"])))

    # отверстия: группы по виду и диаметру
    holes = defaultdict(list)
    info = {}
    for p in prims:
        if p["type"] != "prism" or p.get("rot") or p.get("frame") or p["mode"] != "cut":
            continue
        view = VIEW_OF_PLANE[p["plane"]]
        through = math.isinf(p["d0"]) or math.isinf(p["d1"])
        for c in p["contours"]:
            if c["kind"] != "circle":
                continue
            g = plane_to_global(p["plane"], c["c"][0], c["c"][1], p["offset"])
            key = (view, round(2 * c["r"], 3))
            holes[key].append(to_view(view, *g))
            info[key] = (p["plane"], through, p["d1"] - p["d0"])
    k = 0
    for (view, d), pts in holes.items():
        k += 1
        uniq = sorted(set((round(u, 3), round(v, 3)) for u, v in pts))
        n = len(uniq)
        plane, through, depth = info[(view, d)]
        bu0, bu1, bv0, bv1 = boxes[view]
        cu_box, cv_box = (bu0 + bu1) / 2, (bv0 + bv1) / 2
        central = n == 1 and abs(uniq[0][0] - cu_box) < 1e-3 and abs(uniq[0][1] - cv_box) < 1e-3
        kind_txt = "сквозн." if through else f"глубиной {fmt(depth)}"
        cu, cv = uniq[0]
        bc = ((bu0 + bu1) / 2, (bv0 + bv1) / 2)
        ang = 45.0 if central else math.degrees(math.atan2(cv - bc[1], cu - bc[0]))  # полка — наружу от центра детали
        items.append({"id": f"H{k}", "view": view, "hint": "эксплуатационный" if central else "установочный",
                      "desc": f"отверстия Ø{fmt(d)} {kind_txt}, {n} шт., ось ⟂ плоскости {plane}"
                              + (", одно, по центру детали" if central else ""),
                      "dims": [{"kind": "diam", "c": (cu, cv), "r": d / 2, "prefix": (f"{n} отв. " if n > 1 else ""),
                                "angle": ang}]})
        us_, vs_ = set(u for u, _ in uniq), set(v for _, v in uniq)
        grid = len(us_) * len(vs_) == n  # отверстия в узлах прямоугольной сетки → межосевые по осям
        cyc = _concyclic(uniq)
        if cyc and not (grid and not any(math.dist(cyc[0], c) < 1e-3 for c in round_centers.get(view, []))):
            (pu, pv), R = cyc
            items.append({"id": f"H{k}c", "view": view, "hint": "установочный",
                          "desc": f"диаметр окружности центров {n} отв. Ø{fmt(d)}: Ø{fmt(2 * R)}",
                          "dims": [{"kind": "diam", "c": (pu, pv), "r": R, "prefix": "", "pcd": True, "angle": 120.0}]})
        elif n >= 2:
            us = sorted(set(u for u, _ in uniq))
            vs = sorted(set(v for _, v in uniq))
            if len(us) > 1:
                items.append({"id": f"H{k}u", "view": view, "hint": "установочный",
                              "desc": f"межосевое расстояние отв. Ø{fmt(d)} по горизонтали вида = {fmt(us[-1] - us[0])}",
                              "dims": [{"kind": "lin", "p1": (us[0], bv1), "p2": (us[-1], bv1), "side": "above", "level": None}]})
            if len(vs) > 1:
                items.append({"id": f"H{k}v", "view": view, "hint": "установочный",
                              "desc": f"межосевое расстояние отв. Ø{fmt(d)} по вертикали вида = {fmt(vs[-1] - vs[0])}",
                              "dims": [{"kind": "lin", "p1": (bu1, vs[0]), "p2": (bu1, vs[-1]), "side": "right", "level": None}]})
        if not central:
            pos = []
            for u in sorted(set(u for u, _ in uniq if abs(u - bu0) > 1e-6))[:1]:
                pos.append({"kind": "lin", "p1": (bu0, bv1), "p2": (u, bv1), "side": "above", "level": None})
            for v in sorted(set(v for _, v in uniq if abs(v - bv1) > 1e-6), reverse=True)[:1]:
                pos.append({"kind": "lin", "p1": (bu1, bv1), "p2": (bu1, v), "side": "right", "level": None})
            if pos:
                items.append({"id": f"H{k}p", "view": view, "hint": "установочный",
                              "desc": f"привязка первого отв. Ø{fmt(d)} к кромкам детали", "dims": pos})

    # выступающие цилиндры (бобышки, ступицы) на плоскости XY
    b = 0
    for p in prims:
        if p["type"] == "prism" and p["mode"] == "add" and p["plane"] == "XY" and not p.get("rot") and not p.get("frame"):
            for c in p["contours"][:1]:
                if c["kind"] == "circle":
                    b += 1
                    g = to_view("top", c["c"][0], c["c"][1], 0)
                    items.append({"id": f"B{b}", "view": "top", "hint": "присоединительный",
                                  "desc": f"наружный диаметр цилиндра Ø{fmt(2 * c['r'])}, высота {fmt(p['d1'] - p['d0'])}",
                                  "dims": [{"kind": "diam", "c": g, "r": c["r"], "prefix": "", "angle": 150.0}]})

    # тела вращения: диаметры ступеней
    r_i = 0
    for p in prims:
        if p["type"] != "revolve" or p.get("pcircle") or p.get("ploop") or p.get("axis", "Z") != "Z":
            continue
        prof = p["profile"]
        if p["mode"] == "cut":  # осевое отверстие видно кружком на виде сверху — там и ставим диаметр
            rr = max(r for r, _ in prof)
            if rr > 1e-6:
                r_i += 1
                items.append({"id": f"R{r_i}", "view": "top", "hint": "эксплуатационный",
                              "desc": f"диаметр осевого отверстия Ø{fmt(2 * rr)}",
                              "dims": [{"kind": "diam", "c": to_view("top", p["cx"], p["cy"], 0), "r": rr,
                                        "prefix": "", "angle": 45.0}]})
            continue
        seen = set()
        for i in range(len(prof)):
            (r1, z1), (r2, z2) = prof[i], prof[(i + 1) % len(prof)]
            if abs(r1 - r2) < 1e-6 and r1 > 1e-6 and abs(z2 - z1) > 1e-6 and round(r1, 3) not in seen:
                seen.add(round(r1, 3))
                r_i += 1
                zt = max(z1, z2)
                items.append({"id": f"R{r_i}", "view": "front",
                              "hint": "присоединительный" if p["mode"] == "add" else "эксплуатационный",
                              "desc": f"{'наружный диаметр ступени' if p['mode'] == 'add' else 'диаметр осевого отверстия'} "
                                      f"Ø{fmt(2 * r1)}, длина {fmt(abs(z2 - z1))}",
                              "dims": [{"kind": "lin", "p1": (p["cy"] - r1, zt), "p2": (p["cy"] + r1, zt),
                                        "side": "above", "level": None, "diameter": True}]})

    # карманы и пазы
    q = 0
    for p in prims:
        if p["type"] != "prism" or p.get("rot") or p.get("frame") or p["mode"] != "cut":
            continue
        view = VIEW_OF_PLANE[p["plane"]]
        for c in p["contours"]:
            if c["kind"] != "loop":
                continue
            pts = [s_[1] for s_ in c["segs"]] + [s_[2] for s_ in c["segs"] if s_[0] == "L"]
            us, vs = [t[0] for t in pts], [t[1] for t in pts]
            a = to_view(view, *plane_to_global(p["plane"], min(us), min(vs), p["offset"]))
            bb = to_view(view, *plane_to_global(p["plane"], max(us), max(vs), p["offset"]))
            ul, uh, vl, vh = min(a[0], bb[0]), max(a[0], bb[0]), min(a[1], bb[1]), max(a[1], bb[1])
            q += 1
            items.append({"id": f"P{q}", "view": view, "hint": "эксплуатационный",
                          "desc": f"паз/карман {fmt(uh - ul)} × {fmt(vh - vl)} (плоскость {p['plane']})",
                          "dims": [{"kind": "lin", "p1": (ul, vh), "p2": (uh, vh), "side": "inside", "level": None},
                                   {"kind": "lin", "p1": (uh, vl), "p2": (uh, vh), "side": "inside_v", "level": None}]})
    # круглая в плане деталь: габарит в плане — это диаметр (вместо двух одинаковых линейных)
    dx, dy = hi[0] - lo[0], hi[1] - lo[1]
    ctr = to_view("top", (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, 0)
    for it in items:
        if it["id"][0] != "B":
            continue
        d = it["dims"][0]
        if abs(2 * d["r"] - dx) < 1e-3 and abs(dx - dy) < 1e-3 and math.dist(d["c"], ctr) < 1e-3:
            items = [x for x in items if x["id"] not in ("G1", "G3")]
            it["id"], it["hint"] = "G1", "габаритный"
            it["desc"] = f"габарит в плане — диаметр Ø{fmt(dx)}"
            break
    return {"lo": list(lo), "hi": list(hi), "boxes": boxes, "items": items}


def mandatory_holes(items: list[dict]) -> dict:
    """Каждое отверстие: диаметр (с количеством) и положение — межосевые/окружность центров, иначе привязка к кромкам.
    Осевые отверстия тел вращения — диаметр (положение задаёт ось)."""
    keep = {}
    groups = {it["id"] for it in items if it["id"].startswith("H") and it["id"][1:].isdigit()}
    ids = {it["id"]: it for it in items}
    for g in groups:
        it = ids[g]
        keep[g] = it["hint"]
        loc = [x for x in (g + "c", g + "u", g + "v") if x in ids]
        if not loc and g + "p" in ids:
            loc = [g + "p"]
        for x in loc:
            keep[x] = ids[x]["hint"]
    for it in items:
        if it["id"].startswith("R") and "отверстия" in it["desc"]:
            keep[it["id"]] = "эксплуатационный"
    return keep


def heuristic_keep(items: list[dict]) -> dict:
    """Выбор без модели: габариты, отверстия крепления с межосевыми/окружностью центров,
    центральное отверстие и посадочные диаметры. Привязки к кромкам и карманы — не ставим."""
    keep = mandatory_holes(items)
    for it in items:
        i = it["id"]
        if i.startswith("G") or i.startswith("R") or i.startswith("B"):
            keep.setdefault(i, it["hint"])
    return keep


def plan_selected(C: dict, keep: dict, tol_text: str = "±0,2") -> dict:
    """Собирает план чертежа из выбранных размеров; установочные/присоединительные — с предельными отклонениями."""
    dims = defaultdict(list)
    # на каждом виде — свои размеры: высоту ставим на вид слева, если на главном виде есть другие размеры,
    # а на виде слева — нет; пустой вид слева не строим вовсе
    busy = defaultdict(int)
    for it in C["items"]:
        if it["id"] in keep and it["id"] != "G2":
            busy[it["view"]] += len(it["dims"])
    for it in C["items"]:
        if it["id"] not in keep:
            continue
        cat = keep[it["id"]]
        if it["id"] == "G2" and it.get("alt") and busy["front"] and not busy["left"]:
            it = {**it, **it["alt"]}
        for d in it["dims"]:
            d = dict(d)
            d["cat"] = cat
            if cat in ("установочный", "присоединительный"):  # ГОСТ 2.109-73: с предельными отклонениями
                d["suffix"] = tol_text
            dims[it["view"]].append(d)
    for view, lst in dims.items():
        lvl = defaultdict(int)
        for d in lst:
            if d["kind"] != "lin":
                continue
            if d["level"] is None:
                lvl[d["side"]] += 1
                d["level"] = lvl[d["side"]]
            else:
                lvl[d["side"]] = max(lvl[d["side"]], d["level"])
    views = [v for v in ("front", "top", "left") if dims.get(v)]
    return {"lo": C["lo"], "hi": C["hi"], "boxes": C["boxes"], "dims": dict(dims), "views": views}


def dim_line_pos(d: dict, box, s: float, step_mm: float = 8.0):
    """Положение размерной линии (X3, Y3) в координатах вида и ориентация ('h'/'v')."""
    (x1, y1), (x2, y2) = d["p1"], d["p2"]
    u0, u1, v0, v1 = box
    k = (d["level"] + 1) * step_mm / s
    side = d["side"]
    if side == "below":
        return ((x1 + x2) / 2, v0 - k), "h"
    if side == "above":
        return ((x1 + x2) / 2, v1 + k), "h"
    if side == "left":
        return (u0 - k, (y1 + y2) / 2), "v"
    if side == "right":
        return (u1 + k, (y1 + y2) / 2), "v"
    if side == "inside":
        return ((x1 + x2) / 2, y1 + 3 / s), "h"
    return (x1 + 3 / s, (y1 + y2) / 2), "v"  # inside_v


def layout(lo, hi, s: float, fmt_name: str):
    """Центры видов на листе и начало координат каждого вида (X, Y на листе)."""
    dx, dy, dz = [(h - l) for l, h in zip(lo, hi)]
    W = {"front": dy * s, "top": dy * s, "left": dx * s}
    H = {"front": dz * s, "top": dx * s, "left": dz * s}
    top_y = 287 if fmt_name == "A3" else 287
    cf = (20 + 35 + W["front"] / 2, top_y - 30 - H["front"] / 2)
    centers = {
        "front": cf,
        "top": (cf[0], cf[1] - H["front"] / 2 - 40 - H["top"] / 2),
        "left": (cf[0] + W["front"] / 2 + 45 + W["left"] / 2, cf[1]),
    }
    origins = {}
    for v, (cx, cy) in centers.items():
        u0, u1, v0, v1 = view_box(v, lo, hi)
        origins[v] = (cx - s * (u0 + u1) / 2, cy - s * (v0 + v1) / 2)
    right_edge = centers["left"][0] + W["left"] / 2
    return origins, right_edge


def need_size(lo, hi):
    dx, dy, dz = [(h - l) for l, h in zip(lo, hi)]
    return dy + dx, dz + dx  # суммарная ширина и высота видов в мм модели


def pick_format(lo, hi, scales=(5, 4, 2.5, 2, 1, 0.5, 0.4, 0.25, 0.2, 0.1)):
    w, h = need_size(lo, hi)
    for s in scales:
        for name, (W, H) in (("A4", (85, 137)), ("A3", (280, 190))):
            if w * s <= W and h * s <= H:
                return name, s
    return "A3", scales[-1]


def hole_count_text(n: int) -> str:
    return f"{n} отв. " if n > 1 else ""


__all__ = ["plan", "layout", "pick_format", "dim_line_pos", "fmt", "to_view", "math"]
