"""
Локальная оценка агента на своём «скрытом наборе» (tests/tasks.jsonl).
Критерий: объём ±1 %, габарит ±0,2 мм. КОМПАС не нужен.

  python eval.py                         # реальная модель из config.yaml
  python eval.py --model glm-5.3-flash   # сравнить другую модель
  python eval.py --only t03,t12          # отдельные задания
  python eval.py --replay                # без сети: «модель» отвечает эталоном (проверка конвейера)

Отчёт: out/eval_report.json. Гоняйте после каждой правки промпта.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from agent.llm import ReplayLLM, RequestLog
from agent.privacy import Privacy
from agent.refgeom import same_result
from agent.solver import Solver

ROOT = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default=str(ROOT / "tests" / "tasks.jsonl"))
    ap.add_argument("--model")
    ap.add_argument("--n", type=int)
    ap.add_argument("--only")
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--multi", action="store_true", help="три агента (аналитик → конструктор → контролёр)")
    a = ap.parse_args()

    load_dotenv(ROOT / ".env")
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    if a.n:
        cfg["llm"]["n_samples"] = a.n
    tasks = [json.loads(l) for l in Path(a.tasks).read_text(encoding="utf-8").splitlines() if l.strip()]
    if a.only:
        keep = set(a.only.split(","))
        tasks = [t for t in tasks if t["id"] in keep]

    log = RequestLog(ROOT / "logs" / "eval_requests.jsonl")
    if a.replay:
        llm = ReplayLLM({t["id"]: [json.dumps(t["gold"], ensure_ascii=False)] for t in tasks}, log)
    else:
        from agent.llm import LLMClient
        pv = cfg.get("privacy", {})
        llm = LLMClient(cfg, log, Privacy.from_config(pv, ROOT), model=a.model)
    if a.multi and not a.replay:
        from agent.multiagent import MultiAgent
        solver = MultiAgent(llm, cfg)
    else:
        solver = Solver(llm, cfg)

    report, passed = [], 0
    for t in tasks:
        t0 = time.time()
        r = solver.solve(t["text"], t["id"])
        ok = r.get("status") == "ok" and same_result(r["ref"], t["expected"])
        passed += ok
        got = r.get("ref", {})
        print(f"{'PASS' if ok else 'FAIL'} {t['id']}  V={got.get('volume', 0):10.1f} (эталон {t['expected']['volume']:10.1f})  "
              f"габ={[round(x, 2) for x in got.get('bbox_sorted', [])]} (эталон {t['expected']['bbox_sorted']})  "
              f"групп={len(r.get('groups', []))} ошибок={len(r.get('errors', []))}  {time.time() - t0:.1f}s")
        if not ok and r.get("errors"):
            print("      причина:", r["errors"][0][:200])
        report.append({"id": t["id"], "pass": ok, "result": r, "expected": t["expected"], "text": t["text"]})
    print(f"\nИТОГО: {passed}/{len(tasks)} = {100 * passed / max(1, len(tasks)):.0f} %")
    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    (out / "eval_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
