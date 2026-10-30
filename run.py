"""
Агент: текстовое задание → деталь КОМПАС-3D (.m3d).

  python run.py tasks/                  # все .txt в папке → out/<имя>.m3d
  python run.py tasks.txt               # один файл с несколькими заданиями («Задание 1 …», или разделитель ---)
  python run.py tasks/ --no-kompas      # без КОМПАСа: только JSON и независимый расчёт (для отладки)
  python run.py tasks/ --model glm-5.3-flash --n 3

Результаты: out/<задание>.m3d, out/<задание>.json, out/summary.csv, журнал запросов logs/requests.jsonl
(копия журнала кладётся в out/ для сдачи).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from agent.llm import LLMClient, RequestLog
from agent.privacy import Privacy
from agent.multiagent import MultiAgent
from agent.solver import Solver

ROOT = Path(__file__).resolve().parent


def load_tasks(src: Path) -> list[tuple[str, str]]:
    if src.is_dir():
        files = sorted(p for p in src.iterdir() if p.suffix.lower() in (".txt", ".md"))
        return [(p.stem, p.read_text(encoding="utf-8-sig")) for p in files]
    text = src.read_text(encoding="utf-8-sig")
    # разделители: строка «---» или начало строки «Задание 3.» / «Задача №3:» / «Деталь 3)»
    parts = re.split(r"(?im)^\s*(?:-{3,}\s*$|(?:задание|задача|деталь)\s*№?\s*\d+\s*[.:)]?)", text)
    parts = [p.strip() for p in parts if p.strip()]
    return [(f"task_{i + 1:02d}", p) for i, p in enumerate(parts)]


def ask_console(questions: list[str], issues: list[str]):
    """Вопросы аналитика заказчику в консоли. Пустой ответ на первый вопрос — пропустить задание."""
    print("\n   Аналитик не может построить деталь без уточнений. Ответьте (Enter на первом вопросе — пропустить задание):")
    out = []
    for i, q in enumerate(questions):
        ans = input(f"   ? {q}\n     > ").strip()
        if not ans:
            if i == 0:
                return None
            continue
        out.append((q, ans))
    return out or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="папка с .txt или файл с заданиями")
    ap.add_argument("--out", default="out")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--model", help="переопределить модель из config.yaml")
    ap.add_argument("--provider", help="переопределить провайдера из config.yaml")
    ap.add_argument("--n", type=int, help="число вариантов для голосования")
    ap.add_argument("--no-kompas", action="store_true")
    ap.add_argument("--hidden", action="store_true", help="не показывать окно КОМПАСа")
    ap.add_argument("--single", action="store_true", help="один агент (прежний режим, без аналитика и контролёра)")
    ap.add_argument("--rounds", type=int, help="максимум раундов конструктор ↔ контролёр")
    ap.add_argument("--lenient", action="store_true", help="не отклонять задания: снимать неясности допущениями")
    ap.add_argument("--no-ask", action="store_true", help="при отклонении не задавать вопросы в консоли, а пропускать задание")
    a = ap.parse_args()

    load_dotenv(ROOT / ".env")
    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    if a.provider:
        cfg["llm"]["provider"] = a.provider
    if a.n:
        cfg["llm"]["n_samples"] = a.n
    pv = cfg.get("privacy", {})
    privacy = Privacy.from_config(pv, ROOT)
    log = RequestLog(ROOT / cfg.get("log_file", "logs/requests.jsonl"))
    llm = LLMClient(cfg, log, privacy, model=a.model)

    kompas = None
    if not a.no_kompas:
        from agent.kompas_exec import Kompas, load_calibration
        calib_path = ROOT / "kompas_calibration.json"
        if not calib_path.exists():
            print("ВНИМАНИЕ: нет kompas_calibration.json — сначала запустите python kompas_selftest.py")
        kompas = Kompas(load_calibration(calib_path), visible=not a.hidden)

    tasks = load_tasks(Path(a.src))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    mc = cfg.setdefault("multiagent", {}) or {}
    cfg["multiagent"] = mc
    if a.rounds:
        mc["max_rounds"] = a.rounds
    if a.lenient:
        mc["strict"] = False
    multi = mc.get("enabled", True) and not a.single
    if multi:
        # у аналитика и контролёра может быть своя модель (config.yaml → multiagent)
        la = LLMClient(cfg, log, privacy, model=mc["analyst_model"]) if mc.get("analyst_model") else llm
        li = LLMClient(cfg, log, privacy, model=mc["inspector_model"]) if mc.get("inspector_model") else llm
        ask = None if (a.no_ask or not sys.stdin.isatty()) else ask_console
        solver = MultiAgent(llm, cfg, kompas, la, li, ask=ask)
        print(f"режим: 3 агента (аналитик {la.model} → конструктор {llm.model} → контролёр {li.model}), "
              f"раундов до {solver.max_rounds}")
    else:
        solver = Solver(llm, cfg, kompas)
        print("режим: один агент")
    print(f"заданий: {len(tasks)}, модель: {llm.model}, вариантов: {cfg['llm']['n_samples']}")

    rows = []
    for tid, text in tasks:
        t0 = time.time()
        print(f"\n=== {tid} ===\n{text.strip()}")
        try:
            res = solver.solve(text, tid, str(out / f"{tid}.m3d") if kompas else None)
        except Exception as e:  # одно задание не должно ронять весь прогон
            res = {"task": tid, "status": "crash", "error": f"{type(e).__name__}: {e}"}
        res["seconds"] = round(time.time() - t0, 1)
        res["text"] = text
        (out / f"{tid}.json").write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        ref = res.get("ref") or {}
        km = res.get("kompas") or {}
        rows.append({"task": tid, "status": res.get("status"), "groups": len(res.get("groups", [])),
                     "top_votes": (res.get("groups") or [{}])[0].get("size"),
                     "ref_volume": round(ref.get("volume", 0), 2), "ref_bbox": ref.get("bbox_sorted"),
                     "kompas_volume": km.get("volume"), "kompas_bbox": km.get("bbox_sorted"),
                     "agree": res.get("kompas_agree"), "file": res.get("file"), "sec": res["seconds"],
                     "accepted": res.get("accepted"), "round": res.get("chosen_round"),
                     "warning": res.get("warning", "")})
        print({k: rows[-1][k] for k in ("status", "top_votes", "ref_volume", "ref_bbox", "accepted", "agree", "file")})
        if res.get("status") != "ok" and res.get("errors"):
            print("   причина:", str(res["errors"][0])[:300])

    with (out / "summary.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(rows)
    if log.path.exists():
        shutil.copy(log.path, out / "журнал_запросов.jsonl")
    print(f"\nготово: {out.resolve()}")
    rej = [r["task"] for r in rows if r["status"] == "rejected"]
    if rej:
        print("отклонены аналитиком (вопросы — в <задание>.json, поле questions):", ", ".join(rej))
    return 0 if all(r["status"] == "ok" for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
