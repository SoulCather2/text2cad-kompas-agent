"""
Решение одного задания:
  1. N запросов к модели (один при temperature 0, остальные с разбросом) → N программ на DSL;
  2. каждая программа проверяется и считается независимым расчётом (refgeom);
  3. голосование: результаты группируются по (объём, габарит), побеждает самая большая группа
     (+ бонус за совпадение с габаритами, которые модель сама вывела из задания, и с числами из текста);
  4. если вариантов нет или габарит расходится с ожидаемым — запрос на исправление с конкретной ошибкой;
  5. победитель строится в КОМПАСе, объём и габарит КОМПАСа сверяются с независимым расчётом.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .dsl import DSLError, normalize
from .refgeom import measure, same_result

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_system_prompt() -> str:
    return (PROMPT_DIR / "system.md").read_text(encoding="utf-8")


def user_message(text: str) -> str:
    return f"Задание:\n{text.strip()}\n\nВерни только JSON."


def extract_json(text: str):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        return json.loads(t[a:b + 1])
    raise DSLError("в ответе нет JSON-объекта")


def text_numbers(text: str) -> list[float]:
    t = text.replace("×", " ").replace("х", " ").replace("x", " ")
    nums = [float(s.replace(",", ".")) for s in re.findall(r"\d+(?:[.,]\d+)?", t)]
    return sorted(set(nums))


def text_score(bbox_sorted: list[float], nums: list[float]) -> float:
    """Доля габаритов, которые совпадают с числом из задания или суммой двух чисел."""
    cand = set(nums) | {a + b for a in nums for b in nums}
    return sum(any(abs(d - c) < 0.05 for c in cand) for d in bbox_sorted) / 3


def evaluate(raw: str) -> dict:
    """Ответ модели → кандидат (с измерениями) или ошибка."""
    c = {"raw": raw}
    if raw.startswith("__LLM_ERROR__"):
        c["error"] = "ошибка запроса к модели: " + raw[len("__LLM_ERROR__"):].strip()
        return c
    try:
        spec = extract_json(raw)
        prims = normalize(spec)
        m = measure(prims)
        if m["volume"] <= 0:
            raise DSLError("тело получилось пустым (объём 0) — проверь, не вырезан ли весь материал")
        c.update(spec=spec, prims=prims, m=m)
        exp = spec.get("bbox_expected") if isinstance(spec, dict) else None
        if isinstance(exp, list) and len(exp) == 3 and all(isinstance(x, (int, float)) for x in exp):
            c["self_ok"] = all(abs(a - b) <= 0.2 for a, b in zip(sorted(exp), m["bbox_sorted"]))
            c["bbox_expected"] = sorted(float(x) for x in exp)
    except (DSLError, json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
        c["error"] = f"{type(e).__name__}: {e}"
    return c


def cluster(cands: list[dict], nums: list[float]) -> list[dict]:
    groups: list[dict] = []
    for i, c in enumerate(cands):
        if "m" not in c:
            continue
        for g in groups:
            if same_result(g["members"][0]["m"], c["m"], vol_tol=0.003, bbox_tol=0.05):
                g["members"].append(c)
                break
        else:
            groups.append({"members": [c]})
    for g in groups:
        rep = g["members"][0]
        g["score"] = (len(g["members"])
                      + 0.6 * sum(bool(c.get("self_ok")) for c in g["members"]) / len(g["members"])
                      + 0.4 * text_score(rep["m"]["bbox_sorted"], nums)
                      + 0.1 * any(c.get("t0") for c in g["members"]))
    return sorted(groups, key=lambda g: -g["score"])


class Solver:
    def __init__(self, llm, cfg: dict, kompas=None):
        self.llm = llm
        self.cfg = cfg["llm"]
        self.kompas = kompas
        self.system = load_system_prompt()

    def _ask(self, msgs, temperature, task_id):
        try:
            return self.llm.chat(msgs, temperature, task_id)
        except Exception as e:  # сеть/лимиты — не роняем всё задание
            return f"__LLM_ERROR__ {type(e).__name__}: {e}"

    def generate(self, text: str, task_id: str) -> list[dict]:
        n = int(self.cfg.get("n_samples", 5))
        msgs = [{"role": "system", "content": self.system}, {"role": "user", "content": user_message(text)}]
        temps = [self.cfg.get("temperature_main", 0.0)] + [self.cfg.get("temperature_vote", 0.7)] * (n - 1)
        with ThreadPoolExecutor(max_workers=min(n, int(self.cfg.get("parallel", 5)))) as ex:
            raws = list(ex.map(lambda t: self._ask(msgs, t, task_id), temps))
        cands = [evaluate(r) for r in raws]
        cands[0]["t0"] = True
        return cands

    def repair(self, text: str, cand: dict, problem: str, task_id: str) -> dict:
        msgs = [{"role": "system", "content": self.system},
                {"role": "user", "content": user_message(text)},
                {"role": "assistant", "content": cand["raw"]},
                {"role": "user", "content": problem + "\nИсправь и верни полный исправленный JSON."}]
        c = evaluate(self._ask(msgs, self.cfg.get("temperature_repair", 0.3), task_id))
        c["repair"] = True
        return c

    def solve(self, text: str, task_id: str, out_m3d: str | None = None,
              nums_from: str | None = None, keep_groups: bool = False) -> dict:
        nums = text_numbers(nums_from or text)
        cands = self.generate(text, task_id)
        rounds = int(self.cfg.get("max_repair_rounds", 2))

        # исправление: (а) ни одного рабочего варианта; (б) габарит не совпал с ожидаемым
        for _ in range(rounds):
            groups = cluster(cands, nums)
            if not groups:
                bad = next((c for c in cands if "raw" in c and not c["raw"].startswith("__LLM_ERROR__")), None)
                if bad is None:
                    break
                cands.append(self.repair(text, bad, f"Ошибка построения: {bad.get('error')}", task_id))
                continue
            top = groups[0]["members"][0]
            if top.get("self_ok") is False and len(groups[0]["members"]) < len(cands) * 0.6:
                m = top["m"]
                cands.append(self.repair(
                    text, top,
                    f"Модель по твоим операциям получилась с габаритами {[round(x, 2) for x in m['bbox_sorted']]} мм "
                    f"и объёмом {m['volume']:.1f} мм³, а ты ожидал габариты {top['bbox_expected']}. "
                    "Перепроверь толкование задания и операции.", task_id))
                continue
            break

        groups = cluster(cands, nums)
        result = {"task": task_id, "n_candidates": len(cands),
                  "errors": [c["error"] for c in cands if "error" in c],
                  "groups": [{"size": len(g["members"]), "score": round(g["score"], 2),
                              "volume": round(g["members"][0]["m"]["volume"], 2),
                              "bbox": [round(x, 3) for x in g["members"][0]["m"]["bbox_sorted"]]} for g in groups]}
        if not groups:
            result["status"] = "failed"
            return result

        chosen = groups[0]["members"][0]
        result.update(status="ok", spec=chosen["spec"], ref=chosen["m"])
        if keep_groups:
            result["_groups"] = groups

        if self.kompas is not None and out_m3d:
            result.update(self._build_in_kompas(groups, out_m3d))
        return result

    def _build_in_kompas(self, groups, out_m3d) -> dict:
        """Строим лучший вариант; если КОМПАС расходится с расчётом — пробуем следующие."""
        from .kompas_exec import BuildError

        tried, fallback = [], None
        order = [c for g in groups for c in g["members"]][:6]
        for c in order:
            k = self.kompas
            try:
                k.build(c["prims"])
                km = k.measure()
            except BuildError as e:
                tried.append({"error": str(e)})
                k.close()
                continue
            agree = (km.get("volume") is not None and km.get("bbox_sorted") is not None
                     and same_result(km, c["m"], vol_tol=0.002, bbox_tol=0.05))
            tried.append({"kompas": km, "agree": agree})
            if agree or c["m"]["approx"]:
                path = k.save(out_m3d)
                k.close()
                return {"kompas": km, "kompas_agree": agree, "file": path, "kompas_tries": tried,
                        "spec": c["spec"], "ref": c["m"],
                        "reopen_ok": k.verify_reopen(path)}
            if fallback is None:
                path = k.save(out_m3d)  # сохраняем на всякий случай, перезапишется лучшим
                fallback = (c, km, path)
            k.close()
        if fallback:
            return {"kompas": fallback[1], "kompas_agree": False, "file": fallback[2],
                    "kompas_tries": tried, "spec": fallback[0]["spec"], "ref": fallback[0]["m"],
                    "warning": "КОМПАС не совпал с независимым расчётом ни для одного варианта"}
        return {"kompas_tries": tried, "warning": "КОМПАС не смог построить ни один вариант"}
