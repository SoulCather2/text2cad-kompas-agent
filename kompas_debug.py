"""
Пошаговая сверка КОМПАС ↔ точный расчёт для детали из JSON (результат run.py).
  py kompas_debug.py out_box/01_korpus.json
Строит примитивы по одному и после каждого сравнивает объём. Пришлите вывод целиком.
"""
import json
import sys

from agent.dsl import normalize
from agent.kompas_exec import Kompas, load_calibration
from agent.refgeom import measure


def short(p):
    if p["type"] == "prism":
        c = p["contours"][0]
        what = "круг" if c["kind"] == "circle" else "контур"
        return f"{p['mode']} {p['plane']} offset={p['offset']} d0={p['d0']} d1={p['d1']} {what}×{len(p['contours'])}"
    return f"{p['type']} {p.get('mode', '')}"


def main():
    r = json.load(open(sys.argv[1], encoding="utf-8"))
    prims = normalize(r["spec"])
    cal = load_calibration()
    print("калибровка:", cal["planes"])
    k = Kompas(cal)
    prev_k = prev_r = 0.0
    for i in range(1, len(prims) + 1):
        ref = measure(prims[:i])["volume"]
        try:
            k.build(prims[:i])
            kv = k.measure()["volume"]
        except Exception as e:
            print(f"{i:2d} ОШИБКА {short(prims[i - 1])}: {e}")
            k.close()
            continue
        k.close()
        dk, dr = kv - prev_k, ref - prev_r
        flag = "OK " if abs(dk - dr) < max(1.0, 0.02 * abs(dr)) else "BAD"
        print(f"{i:2d} {flag} {short(prims[i - 1]):60s} ΔКОМПАС={dk:9.1f}  Δрасчёт={dr:9.1f}")
        prev_k, prev_r = kv, ref


if __name__ == "__main__":
    main()
