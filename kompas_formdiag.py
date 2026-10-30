"""
Проверка новых операций формообразования в КОМПАСе и автоподбор их настроек.
Запуск (КОМПАС открыт):  py kompas_formdiag.py

Строит тестовые детали: горизонтальные валы, наклонные плоскости, трубы по траектории, тела по сечениям,
выдавливание с уклоном, оболочки — и сверяет объём и габарит КОМПАСа с точным расчётом.
Если что-то не совпало — перебирает настройки (направление уклона, режим кинематики, способ построения
наклонной плоскости) и сохраняет подходящие в kompas_calibration.json.
Пришлите весь вывод, если в конце останутся строки BAD/ERR.
"""
import json
import traceback

from agent.dsl import normalize
from agent.kompas_exec import Kompas, load_calibration
from agent.refgeom import measure
from tests.cases import CASES

CALIB_FILE = "kompas_calibration.json"
GROUPS = {
    "вращение вокруг X/Y": ("горизонт",),
    "наклонные плоскости": ("наклон",),
    "кинематика (трубы)": ("траектор",),
    "по сечениям": ("сечени",),
    "уклон": ("уклон",),
    "оболочка": ("оболоч",),
}


def run_case(k, spec):
    prims = normalize(spec)
    ref = measure(prims)
    k.build(prims)
    m = k.measure()
    k.close()
    dv = (m["volume"] - ref["volume"]) / ref["volume"]
    db = max(abs(a - b) for a, b in zip(m["bbox_sorted"], ref["bbox_sorted"]))
    tol = 0.01 if ref["approx"] else 0.002
    return abs(dv) < tol and db < 0.05, dv, db, ref["approx"]


def check_group(k, keys, label):
    cases = [c for c in CASES if any(w in c[0] for w in keys)]
    bad = 0
    for name, spec, *_ in cases:
        try:
            ok, dv, db, approx = run_case(k, spec)
            print(f"  {'OK ' if ok else 'BAD'} {name:55s} dV={dv * 100:+.3f}%  dBbox={db:.3f}"
                  + ("  (сопоставление сечений приблизительное, допуск 1 %)" if approx else ""))
            bad += not ok
        except Exception as e:
            bad += 1
            print(f"  ERR {name}: {type(e).__name__}: {e}")
            try:
                k.close()
            except Exception:
                pass
    return bad


def main():
    calib = load_calibration(CALIB_FILE)
    k = Kompas(calib)
    tries = {
        "уклон": [("draft_flip", v) for v in (False, True)],
        "кинематика (трубы)": [("sweep", {"shift": v}) for v in (calib.get("spring", {}).get("shift", 1), 0, 1, 2)],
    }
    total_bad = 0
    for label, keys in GROUPS.items():
        print(f"\n== {label}")
        bad = check_group(k, keys, label)
        if bad and label in tries:
            for key, val in tries[label]:
                if calib.get(key) == val:
                    continue
                print(f"  … пробую {key} = {val}")
                calib[key] = val
                k.calib = calib
                k._frame_cache = {}
                bad = check_group(k, keys, label)
                if not bad:
                    print(f"  найдено: {key} = {val}")
                    break
        if label == "наклонные плоскости":
            fc = getattr(k, "_frame_cache", {}) or {}
            hows = {v[0] for v in fc.values()}
            if len(hows) == 1:
                calib["frame_how"] = hows.pop()
                print(f"  наклонные плоскости строятся способом: {calib['frame_how']}")
        total_bad += bad
    with open(CALIB_FILE, "w", encoding="utf-8") as f:
        json.dump(calib, f, ensure_ascii=False, indent=2)
    print("\nВСЁ СОВПАЛО — настройки сохранены" if total_bad == 0 else
          f"\nрасхождений: {total_bad} — пришлите вывод целиком")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
