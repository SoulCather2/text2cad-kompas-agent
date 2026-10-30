"""
Калибровка пружины в КОМПАСе.  Запуск (КОМПАС открыт):  py kompas_spring.py
Строит пружину D22 d2 шаг 6, 8 витков с разными настройками спирали и кинематической операции,
сравнивает объём и габарит с точным расчётом и сохраняет подходящие настройки в kompas_calibration.json.
"""
import itertools
import json

from agent.dsl import normalize
from agent.kompas_exec import Kompas, load_calibration
from agent.refgeom import measure

SPEC = {"ops": [{"op": "spring", "D": 22, "d": 2, "pitch": 6, "n": 8}]}


def main():
    calib = load_calibration()
    k = Kompas(calib)
    prims = normalize(SPEC)
    ref = measure(prims)
    print(f"нужно: V = {ref['volume']:.1f}, габарит {ref['bbox_sorted']}")
    found = None
    for bm, shift, bdir in itertools.product((0, 1, 2), (1, 0, 2), (True, False)):
        calib["spring"] = {"buildMode": bm, "shift": shift, "buildDir": bdir}
        k.calib = calib
        try:
            k.build(prims)
            m = k.measure()
        except Exception as e:
            print(f"  buildMode={bm} shift={shift} buildDir={bdir}: ошибка {e}")
            k.close()
            continue
        k.close()
        v, bb = m.get("volume"), m.get("bbox_sorted")
        ok = v is not None and bb is not None and abs(v - ref["volume"]) / ref["volume"] < 0.005 \
            and all(abs(a - b) < 0.2 for a, b in zip(bb, ref["bbox_sorted"]))
        print(f"  buildMode={bm} shift={shift} buildDir={bdir}: V={v and round(v, 1)} габарит={bb and [round(x, 2) for x in bb]}{'  ✓' if ok else ''}")
        if ok:
            found = calib["spring"]
            break
    if found:
        with open("kompas_calibration.json", "w", encoding="utf-8") as f:
            json.dump(calib, f, ensure_ascii=False, indent=2)
        print("найдено и сохранено:", found)
    else:
        print("подходящих настроек нет — пришлите вывод целиком")


if __name__ == "__main__":
    main()
