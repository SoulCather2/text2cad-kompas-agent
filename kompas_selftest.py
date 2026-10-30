"""
Калибровка и проверка исполнителя КОМПАС-3D. Запускать на ноутбуке с КОМПАСом:

    python kompas_selftest.py            # калибровка + сверка всех тестовых деталей
    python kompas_selftest.py --check    # только сверка (калибровка уже есть)

1) Единицы объёма: кубик 10 мм → коэффициент volume_scale.
2) Плоскости: перебирает знаки осей эскиза/нормали/смещения для XY, XZ, YZ и находит
   вариант, при котором КОМПАС даёт тот же объём, что и независимый расчёт.
3) Сверка: все детали из tests/cases.py строятся в КОМПАСе и сравниваются с эталоном.
Результат калибровки → kompas_calibration.json (его подхватывает агент).
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import traceback

from agent.dsl import normalize
from agent.kompas_exec import DEFAULT_CALIB, Kompas, load_calibration
from agent.refgeom import measure
from tests.cases import CASES

CALIB_FILE = "kompas_calibration.json"


def kmeasure(k: Kompas, spec) -> dict:
    k.build(normalize(spec))
    m = k.measure()
    k.close()
    return m


# Брусок 12 (X) × 12 (Y) × 6 (Z) и несимметричный прямоугольник u 2..10, v 1..3:
# любая ошибка знака, перестановка осей эскиза или неверное направление меняют объём.
NORMAL_TOP = {"XY": 6, "XZ": 12, "YZ": 12}


def _block():
    return {"op": "extrude", "sketch": [{"type": "rect", "x0": 0, "y0": 0, "w": 12, "h": 12}], "depth": 6}


def probe_cut(plane: str) -> dict:
    """ВЫРЕЗ 8×2 на плоскости plane, смещение = верх − 2, глубина 3 в '+' (обрезается гранью → 2 мм)."""
    return {"ops": [_block(), {"op": "cut", "plane": plane, "offset": NORMAL_TOP[plane] - 2, "direction": "+",
                               "depth": 3, "sketch": [{"type": "rect", "x0": 2, "y0": 1, "w": 8, "h": 2}]}]}


def probe_add(plane: str) -> dict:
    """БОБЫШКА 8×2 там же, высота 3 в '+' (выступает на 1 мм)."""
    return {"ops": [_block(), {"op": "extrude", "plane": plane, "offset": NORMAL_TOP[plane] - 2, "direction": "+",
                               "depth": 3, "sketch": [{"type": "rect", "x0": 2, "y0": 1, "w": 8, "h": 2}]}]}


ROT_ANGLE = {"XZ": 90, "YZ": -90}  # при таком повороте пробный вырез попадает в брусок


def probe_rot_cut(plane: str) -> dict:
    """ВЫРЕЗ на плоскости plane, повёрнутой вокруг Z (круговой массив с одним углом)."""
    return {"ops": [_block(), {"op": "circular_pattern", "angles": [ROT_ANGLE[plane]], "ops": [
        {"op": "cut", "plane": plane, "offset": -10, "direction": "+", "depth": 3,
         "sketch": [{"type": "rect", "x0": 2, "y0": 1, "w": 8, "h": 2}]}]}]}


def probe_rot_add(plane: str) -> dict:
    """БОБЫШКА на повёрнутой плоскости, выступает за брусок на 2 мм."""
    return {"ops": [_block(), {"op": "circular_pattern", "angles": [ROT_ANGLE[plane]], "ops": [
        {"op": "extrude", "plane": plane, "offset": -11, "direction": "-", "depth": 3,
         "sketch": [{"type": "rect", "x0": 2, "y0": 1, "w": 8, "h": 2}]}]}]}


ROT30 = {"XZ": 30, "YZ": -30}  # «несимметричный» угол: ловит неверный отсчёт угла


def probe_rot30(plane: str) -> dict:
    """БОБЫШКА 18×2 толщиной 1 на плоскости, повёрнутой на ±30°: торчит из бруска,
    поэтому габарит по X и Y зависит от угла (сверяем габарит по каждой оси)."""
    return {"ops": [_block(), {"op": "circular_pattern", "angles": [ROT30[plane]], "ops": [
        {"op": "extrude", "plane": plane, "offset": 0.5, "direction": "+", "depth": 1,
         "sketch": [{"type": "rect", "x0": 2, "y0": 1, "w": 18, "h": 2}]}]}]}


def try_combo(k, calib, plane, combo, probe) -> bool:
    calib["planes"][plane] = dict(combo)
    base = plane.rstrip("r")
    ref = measure(normalize(probe(base)))
    try:
        km = kmeasure(k, probe(base))
        v = km["volume"]
    except Exception as e:
        print(f"  {plane} {combo}: ошибка {e}")
        k.close()
        return False
    ok = abs(v - ref["volume"]) < 0.5
    if ok and km.get("bbox"):  # габарит по каждой оси (не отсортированный)
        ok = all(abs(a - b) < 0.05 for a, b in zip(km["bbox"], ref["bbox"]))
    print(f"  {plane} {probe.__name__} {combo}: V={v:.2f} (нужно {ref['volume']:.2f}){'  ✓' if ok else ''}")
    return ok


def calibrate() -> dict:
    calib = json.loads(json.dumps(DEFAULT_CALIB))
    k = Kompas(calib)
    m = kmeasure(k, {"ops": [{"op": "extrude", "sketch": [{"type": "rect", "w": 10, "h": 10}], "depth": 10}]})
    print("кубик 10 мм:", m)
    if not m.get("volume"):
        sys.exit("КОМПАС не вернул объём — поправьте Kompas.measure() (см. ошибки выше)")
    calib["volume_scale"] = 1000.0 / m["volume"]
    print(f"volume_scale = {calib['volume_scale']:.6g}")
    k.calib = calib

    for plane in ("XY", "XZ", "YZ"):
        found = None
        if plane == "XY":  # оси эскиза XY совпадают с глобальными; sn влияет и на кубик
            combos = [{"su": 1, "sv": 1, "sn": sn, "so": so, "sc": sc, "swap": 0}
                      for sn in (1, -1) for so in (1, -1) for sc in (-1, 1)]
            for c in combos:
                if try_combo(k, calib, plane, c, probe_cut) and try_combo(k, calib, plane, c, probe_add):
                    found = c
                    break
        else:  # сначала вырез (su, sv, so, sc), потом бобышка (sn)
            for swap, su, sv, so, sc in itertools.product((0, 1), (1, -1), (1, -1), (1, -1), (-1, 1)):
                c = {"su": su, "sv": sv, "sn": 1, "so": so, "sc": sc, "swap": swap}
                if not try_combo(k, calib, plane, c, probe_cut):
                    continue
                for sn in (1, -1):
                    c2 = {**c, "sn": sn, "sc": sc * sn}  # знак выреза = sn·sc — держим его прежним
                    if try_combo(k, calib, plane, c2, probe_add) and try_combo(k, calib, plane, c2, probe_cut):
                        found = c2
                        break
                if found:
                    break
        if not found:
            sys.exit(f"не удалось откалибровать плоскость {plane} — пришлите вывод целиком")
        calib["planes"][plane] = found
        print(f"{plane}: {found}")

    # плоскости под углом к XZ/YZ калибровать не нужно: модель КОМПАСа задана формулой (kompas_exec._rotated_sketch)
    with open(CALIB_FILE, "w", encoding="utf-8") as f:
        json.dump(calib, f, ensure_ascii=False, indent=2)
    print("сохранено в", CALIB_FILE)
    return calib


def check(calib: dict):
    k = Kompas(calib)
    bad = 0
    for name, spec, *_ in CASES:
        ref = measure(normalize(spec))
        try:
            m = kmeasure(k, spec)
            dv = (m["volume"] - ref["volume"]) / ref["volume"]
            db = max(abs(a - b) for a, b in zip(m["bbox_sorted"], ref["bbox_sorted"]))
            ok = abs(dv) < 0.002 and db < 0.05
            print(f"{'OK ' if ok else 'BAD'} {name:50s} dV={dv * 100:+.3f}%  dBbox={db:.3f}")
            bad += not ok
        except Exception:
            bad += 1
            print(f"ERR {name}")
            traceback.print_exc(limit=1)
            k.close()
    print("ВСЁ СОВПАЛО" if bad == 0 else f"расхождений: {bad} — чините соответствующие методы в agent/kompas_exec.py")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="только сверка, без калибровки")
    a = ap.parse_args()
    check(load_calibration(CALIB_FILE) if a.check else calibrate())
