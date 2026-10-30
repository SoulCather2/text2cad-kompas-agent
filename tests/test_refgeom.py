"""Проверка независимого расчёта на деталях с известным объёмом. Запуск: python -m tests.test_refgeom"""
import math, time
from agent.dsl import normalize
from agent.refgeom import measure
from tests.cases import CASES

ok = True
for name, spec, vexp, bexp in CASES:
    t = time.time()
    m = measure(normalize(spec))
    if vexp is None:  # эталон — сам расчёт (проверяется только в КОМПАСе)
        print(f"--  {name:45s} V={m['volume']:.1f}"); continue
    dv = (m["volume"]-vexp)/vexp
    db = max(abs(a-b) for a,b in zip(m["bbox_sorted"], sorted(bexp))) if bexp else 0.0
    good = abs(dv) < 0.002 and db < 0.02
    ok &= good
    print(f"{'OK ' if good else 'BAD'} {name:45s} V={m['volume']:.1f} exp={vexp:.1f} dV={dv*100:+.3f}% dB={db:.4f} spread={m['spread']*100:.3f}% {time.time()-t:.2f}s")
print("ALL OK" if ok else "FAILURES")
