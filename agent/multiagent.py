"""
Мультиагентная система из трёх агентов с обратной связью.

  Агент 1 «Аналитик»   — проверяет текст задания: полнота, противоречия, неоднозначности;
                          выдаёт уточнённое задание, список допущений и список проверяемых требований.
                          Его ответ проверяется программно: ни одно число из задания не должно потеряться.
  Агент 2 «Конструктор» — строит программу детали (прежний решатель: 5 вариантов, голосование,
                          независимый расчёт геометрии, исправление ошибок).
  Агент 3 «Контролёр»  — сверяет результат с требованиями: факты (габарит, объём, сводка элементов)
                          считаются программно, решение «принять / переделать» — моделью.

Обратная связь:
  контролёр → конструктор  (ошибка построения: замечания + прошлое решение → новый раунд)
  контролёр → аналитик     (неверно понято задание: аналитик пересматривает уточнение → конструктор заново)
  аналитик  ← программа    (потеряны числа из задания → аналитик исправляет)

Итог: последний принятый контролёром вариант; если принятых нет — вариант с наименьшим числом замечаний
(при равенстве — более ранний). В КОМПАСе строится только итоговый вариант.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .refgeom import measure
from .solver import PROMPT_DIR, Solver, extract_json, text_numbers


def _load(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _fmt(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def summarize(prims: list[dict]) -> list[str]:
    """Сводка элементов детали по примитивам — для контролёра (считается программно, без модели)."""
    out, holes = [], Counter()
    for i, p in enumerate(prims):
        t = p["type"]
        if t == "mesh":
            b = p.get("build", {})
            what = {"sweep": f"труба/пруток по траектории Ø{_fmt(2 * b.get('r', 0))}"
                             + (f"/Ø{_fmt(2 * b['ri'])}" if b.get("ri") else "") + f", длина оси {_fmt(b.get('length', 0))}",
                    "loft": f"тело по сечениям между смещениями {_fmt(b.get('off0', 0))} и {_fmt(b.get('off1', 0))}",
                    "draft": f"выдавливание с уклоном {_fmt(b.get('angle', 0))}° на глубину {_fmt(b.get('depth', 0))}"}.get(b.get("kind"), "сложная форма")
            out.append(("вырез: " if p["mode"] == "cut" else "") + what)
            continue
        if t == "prism":
            d0, d1 = p.get("d0", 0), p.get("d1", 0)
            lo, hi = p["offset"] + min(d0, d1), p["offset"] + max(d0, d1)
            span = ("насквозь" if abs(lo) > 1e5 or abs(hi) > 1e5
                    else f"от {_fmt(lo)} до {_fmt(hi)} по нормали к плоскости")
            circles = [c for c in p["contours"] if c["kind"] == "circle"]
            loops = [c for c in p["contours"] if c["kind"] == "loop"]
            if p["mode"] == "cut":
                for c in circles:
                    holes[(p["plane"] + (" с наклоном" if p.get("frame") else ""), _fmt(2 * c["r"]), span)] += 1
                if loops:
                    out.append(f"вырез-контур в плоскости {p['plane']} ({len(loops)} шт.), {span}")
            else:
                what = f"{len(circles)} круг(ов)" if circles and not loops else f"{len(loops)} контур(ов)"
                out.append(f"выдавливание в плоскости {p['plane']}{' с поворотом' if p.get('rot') or p.get('frame') else ''}: "
                           f"{what}, {span}")
        elif t == "revolve":
            ax = p.get("axis", "Z")
            pos = {"Z": ("cx", "cy"), "X": ("cy", "cz"), "Y": ("cx", "cz")}[ax]
            out.append(f"тело вращения ({'вырез' if p['mode'] == 'cut' else 'добавление'}) вокруг оси ‖ {ax} "
                       f"через ({_fmt(p.get(pos[0], 0))}; {_fmt(p.get(pos[1], 0))})")
        elif t == "helix":
            out.append(f"пружина: D={_fmt(2 * p['R'])}, d={_fmt(2 * p['r'])}, шаг {_fmt(p['pitch'])}, витков {_fmt(p['turns'])}")
        elif t == "thread":
            out.append(f"резьба М{_fmt(p['d'])}×{_fmt(p['pitch'])} {'внутренняя' if p['internal'] else 'наружная'}, "
                       f"z {_fmt(p['z0'])}..{_fmt(p['z1'])}")
        elif t == "edge":
            out.append("скругление/фаска кромки")
    for (plane, d, span), n in sorted(holes.items()):
        out.append(f"отверстия Ø{d}: {n} шт., плоскость {plane}, {span}")
    return out


def idle_ops(prims: list[dict], limit: int = 30) -> list[str]:
    """Программная проверка: операции, которые не изменили деталь (вырез мимо материала, выступ внутри тела)."""
    body = [i for i, p in enumerate(prims) if p["type"] in ("prism", "revolve", "mesh")]
    if len(body) < 2 or len(body) > limit:
        return []
    try:
        v_all = measure(prims, n=200)["volume"]
    except Exception:
        return []
    out = []
    for i in body[1:]:
        try:
            v = measure(prims[:i] + prims[i + 1:], n=200)["volume"]
        except Exception:
            continue
        if abs(v - v_all) <= max(1e-3, 1e-5 * v_all):
            p = prims[i]
            what = "вырез" if p["mode"] == "cut" else "выступ"
            where = f"в плоскости {p.get('plane', 'XY')}" if p["type"] == "prism" else "(тело вращения)"
            out.append(f"примитив №{i + 1}: {what} {where} не изменил деталь — "
                       + ("вырезает пустоту (мимо материала)" if p["mode"] == "cut" else "целиком внутри тела"))
    return out


class MultiAgent:
    def __init__(self, llm, cfg: dict, kompas=None, llm_analyst=None, llm_inspector=None, ask=None):
        self.llm = llm
        self.la = llm_analyst or llm
        self.li = llm_inspector or llm
        self.cfg = cfg
        mc = cfg.get("multiagent", {}) or {}
        self.max_rounds = int(mc.get("max_rounds", 3))
        self.use_analyst = mc.get("analyst", True)
        self.use_inspector = mc.get("inspector", True)
        self.strict = mc.get("strict", True)            # отклонять неполные/противоречивые задания
        self.max_questions = int(mc.get("max_questions", 2))  # раундов «вопрос — ответ» с заказчиком
        self.ask = ask                                   # функция(вопросы, замечания) → [(вопрос, ответ)] или None
        self.kompas = kompas
        self.designer = Solver(llm, cfg)          # конструктор работает без КОМПАСа; строим только итог
        self.builder = Solver(llm, cfg, kompas)
        self.p_analyst = _load("analyst.md")
        self.p_inspector = _load("inspector.md")

    # ---------- общий вызов модели с разбором JSON
    def _ask_json(self, llm, system: str, user: str, tag: str, history: list | None = None):
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}] + (history or [])
        try:
            raw = llm.chat(msgs, 0.0, tag)
        except Exception as e:
            return None, f"{type(e).__name__}: {e}", msgs
        try:
            data = extract_json(raw)
            if not isinstance(data, dict):
                raise ValueError("ответ не объект JSON")
            return data, raw, msgs
        except Exception as e:
            return None, f"{type(e).__name__}: {e}", msgs + [{"role": "assistant", "content": raw}]

    # ---------- агент 1
    def analyst(self, text: str, tid: str, feedback: str | None = None, prev: dict | None = None) -> dict:
        user = f"Задание:\n{text.strip()}\n\nВерни только JSON."
        hist = []
        if feedback and prev:
            hist = [{"role": "assistant", "content": json.dumps(prev, ensure_ascii=False)},
                    {"role": "user", "content": feedback + "\nПересмотри и верни полный исправленный JSON."}]
        for attempt in range(2):
            data, raw, msgs = self._ask_json(self.la, self.p_analyst, user, f"{tid}:аналитик", hist)
            if data is None:
                return {"ok": False, "error": raw, "clarified_task": text, "checklist": [], "assumptions": [], "issues": []}
            ct = str(data.get("clarified_task") or "").strip()
            lost = [n for n in text_numbers(text) if n not in text_numbers(ct)] if ct else ["всё"]
            if not lost:
                data["ok"] = True
                data["clarified_task"] = ct
                return data
            # обратная связь программа → аналитик: потеряны числа
            hist = hist + [{"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
                           {"role": "user", "content": f"В clarified_task потеряны числа из задания: "
                                                       f"{[_fmt(x) if isinstance(x, float) else x for x in lost]}. "
                                                       "Все числа исходного задания должны остаться. Верни полный исправленный JSON."}]
        # не справился — безопасный откат: исходный текст + допущения аналитика
        data = data or {}
        data.update(ok=False, error="аналитик терял числа задания — используется исходный текст",
                    clarified_task=text.strip() + ("\n\nДопущения:\n- " + "\n- ".join(data.get("assumptions") or [])
                                                   if data.get("assumptions") else ""))
        return data

    # ---------- агент 2
    def designer_text(self, a: dict, feedback: list[str] | None, prev_spec: dict | None) -> str:
        t = a["clarified_task"]
        if a.get("checklist"):
            t += "\n\nТребования, которые будут проверены:\n- " + "\n- ".join(map(str, a["checklist"]))
        if feedback:
            t += "\n\nКонтролёр нашёл ошибки в прошлой версии — исправь их:\n- " + "\n- ".join(feedback)
            if prev_spec:
                t += "\nПрошлая версия (исправь, а не пиши с нуля):\n" + json.dumps(prev_spec, ensure_ascii=False)
        return t

    # ---------- агент 3
    def facts(self, res: dict, a: dict) -> dict:
        m = res["ref"]
        f = {"габариты_X_Y_Z": [round(x, 2) for x in m["bbox"]], "объём_мм3": round(m["volume"], 1),
             "голосов_за_вариант": res["groups"][0]["size"], "вариантов_всего": res["n_candidates"],
             "элементы": summarize(res["_groups"][0]["members"][0]["prims"])}
        idle = idle_ops(res["_groups"][0]["members"][0]["prims"])
        if idle:
            f["операции_без_эффекта"] = idle
        exp = a.get("bbox_expected")
        if isinstance(exp, list) and len(exp) == 3 and all(isinstance(x, (int, float)) for x in exp):
            ok = all(abs(x - y) <= 0.2 for x, y in zip(sorted(exp), m["bbox_sorted"]))
            f["ожидаемые_габариты_аналитика"] = exp
            f["габариты_совпали"] = ok
        return f

    def inspector(self, text: str, a: dict, res: dict, tid: str) -> dict:
        facts = self.facts(res, a)
        if not self.use_inspector:
            return self._facts_verdict(facts)
        user = (f"Исходное задание:\n{text.strip()}\n\n"
                f"Уточнение аналитика:\n{a['clarified_task']}\n\n"
                f"Требования:\n- " + "\n- ".join(map(str, a.get("checklist") or ["(нет)"])) + "\n\n"
                f"Программа конструктора:\n{json.dumps(res['spec'], ensure_ascii=False)}\n\n"
                f"Факты (точный расчёт):\n{json.dumps(facts, ensure_ascii=False, indent=1)}\n\nВерни только JSON.")
        data, raw, _ = self._ask_json(self.li, self.p_inspector, user, f"{tid}:контролёр")
        if data is None:  # контролёр недоступен — решаем только по фактам
            return {**self._facts_verdict(facts), "error": raw}
        v = "redo" if str(data.get("verdict", "")).lower().startswith("redo") else "accept"
        probs = [str(p) for p in (data.get("problems") or [])] if v == "redo" else []
        if v == "redo" and not probs:
            v = "accept"
        return {"verdict": v, "problems": probs, "facts": facts,
                "fix_target": "analyst" if str(data.get("fix_target", "")).startswith("analyst") else "designer"}

    @staticmethod
    def _facts_verdict(facts: dict) -> dict:
        probs = list(facts.get("операции_без_эффекта", []))
        if facts.get("габариты_совпали") is False:
            probs.append(f"габариты {facts['габариты_X_Y_Z']} не совпали с ожидаемыми {facts['ожидаемые_габариты_аналитика']}")
        return {"verdict": "redo" if probs else "accept", "facts": facts, "fix_target": "designer", "problems": probs}

    # ---------- приёмка задания (строгий режим): ok / warn / reject (+ вопросы заказчику)
    def intake(self, text: str, tid: str, trace: list):
        """Возвращает (задание с уточнениями, ответ аналитика) или (None, ответ) — задание отклонено."""
        for attempt in range(self.max_questions + 1):
            a = self.analyst(text, tid)
            a["verdict_given"] = bool(a.get("verdict"))
            verdict = str(a.get("verdict") or ("ok" if a.get("ok") else "warn")).lower()
            a["verdict"] = verdict
            qs = [str(q) for q in (a.get("questions") or [])]
            trace.append({"agent": "аналитик", "verdict": verdict, "issues": a.get("issues"), "questions": qs,
                          "assumptions": a.get("assumptions"), "checklist": a.get("checklist"),
                          "bbox_expected": a.get("bbox_expected"), "error": a.get("error")})
            label = "АНАЛИТИК НЕДОСТУПЕН, работаем по исходному тексту" if a.get("error") and not a.get("verdict_given") else None
            label = label or {"ok": "ЗАДАНИЕ ПРИНЯТО", "warn": "ПРИНЯТО С ДОПУЩЕНИЯМИ", "reject": "ЗАДАНИЕ ОТКЛОНЕНО"}.get(verdict, verdict)
            print(f"   [1 аналитик] {label}: замечаний {len(a.get('issues') or [])}, допущений {len(a.get('assumptions') or [])}, "
                  f"требований {len(a.get('checklist') or [])}" + (f"  ! {a['error']}" if a.get("error") else ""))
            for i in (a.get("issues") or [])[:6]:
                print(f"        ! {i}")
            if verdict == "warn":
                for i in (a.get("assumptions") or [])[:6]:
                    print(f"        ~ {i}")
            if verdict != "reject" or not self.strict:
                return text, a
            if not qs:
                qs = ["Уточните задание по замечаниям выше."]
            if self.ask is None or attempt == self.max_questions:
                return None, {**a, "questions": qs}
            answers = self.ask(qs, a.get("issues") or [])
            if not answers:
                return None, {**a, "questions": qs}
            text = text.rstrip() + "\n\nУточнения заказчика:\n" + "\n".join(f"- {q} — {ans}" for q, ans in answers)
        return None, a

    # ---------- цикл
    def solve(self, text: str, tid: str, out_m3d: str | None = None) -> dict:
        trace = []
        if self.use_analyst:
            text2, a = self.intake(text, tid, trace)
            if text2 is None:
                print("   ЗАДАНИЕ НЕ ПРИНЯТО. Ответьте на вопросы и допишите их в задание:")
                for q in a.get("questions") or []:
                    print(f"        ? {q}")
                return {"task": tid, "status": "rejected", "agents": trace, "groups": [],
                        "issues": a.get("issues"), "questions": a.get("questions"),
                        "errors": ["задание отклонено аналитиком: " + "; ".join(map(str, a.get("issues") or []))]}
            text = text2
        else:
            a = {"ok": True, "clarified_task": text, "checklist": [], "assumptions": [], "issues": []}

        rounds, feedback, prev_spec = [], None, None
        for rnd in range(1, self.max_rounds + 1):
            dt = self.designer_text(a, feedback, prev_spec)
            res = self.designer.solve(dt, f"{tid}:конструктор", None, nums_from=text, keep_groups=True)
            if res.get("status") != "ok":
                # конструктор не построил ни одного варианта; если раунд не первый — откат к исходному тексту
                trace.append({"agent": "конструктор", "round": rnd, "status": res.get("status"), "errors": res.get("errors", [])[:3]})
                print(f"   [2 конструктор] раунд {rnd}: нет рабочих вариантов")
                if a.get("clarified_task") != text:
                    a = {**a, "clarified_task": text}
                    feedback, prev_spec = None, None
                    continue
                break
            print(f"   [2 конструктор] раунд {rnd}: V={res['ref']['volume']:.1f} габ={[round(x, 2) for x in res['ref']['bbox_sorted']]} "
                  f"голосов {res['groups'][0]['size']}/{res['n_candidates']}")
            trace.append({"agent": "конструктор", "round": rnd, "volume": round(res["ref"]["volume"], 2),
                          "bbox": res["ref"]["bbox_sorted"], "groups": res["groups"]})
            ins = self.inspector(text, a, res, tid)
            trace.append({"agent": "контролёр", "round": rnd, "verdict": ins["verdict"], "problems": ins["problems"],
                          "fix_target": ins["fix_target"], "facts": ins["facts"]})
            print(f"   [3 контролёр] раунд {rnd}: {'ПРИНЯТО' if ins['verdict'] == 'accept' else 'ПЕРЕДЕЛАТЬ'}"
                  + "".join(f"\n        - {p}" for p in ins["problems"][:5]))
            rounds.append((rnd, res, ins))
            if ins["verdict"] == "accept" or rnd == self.max_rounds:
                break
            if ins["fix_target"] == "analyst" and self.use_analyst:
                a = self.analyst(text, tid, "Контролёр считает, что задание понято неверно:\n- " + "\n- ".join(ins["problems"]),
                                 {k: a.get(k) for k in ("issues", "assumptions", "clarified_task", "checklist", "bbox_expected")})
                trace.append({"agent": "аналитик", "round": rnd, "revised": True, "checklist": a.get("checklist"),
                              "assumptions": a.get("assumptions"), "error": a.get("error")})
                print(f"   [1 аналитик] пересмотр задания по замечаниям контролёра")
                feedback, prev_spec = None, None
            else:
                feedback, prev_spec = ins["problems"], res["spec"]

        if not rounds:
            return {"task": tid, "status": "failed", "agents": trace, "groups": [],
                    "errors": next((t.get("errors") for t in reversed(trace) if t.get("errors")), [])}
        acc = [r for r in rounds if r[2]["verdict"] == "accept"]
        rnd, res, ins = acc[-1] if acc else min(rounds, key=lambda r: (len(r[2]["problems"]), r[0]))
        groups = res.pop("_groups")
        for _, r, _ in rounds:
            r.pop("_groups", None)
        result = {**res, "task": tid, "agents": trace, "chosen_round": rnd, "accepted": ins["verdict"] == "accept",
                  "task_text_final": text,
                  "analyst": {k: a.get(k) for k in ("verdict", "issues", "assumptions", "checklist", "bbox_expected")},
                  "clarified_task": a.get("clarified_task")}
        if not result["accepted"]:
            result["warning"] = "контролёр не принял ни один вариант; выбран вариант с наименьшим числом замечаний"
        print(f"   итог: раунд {rnd}, {'принят контролёром' if result['accepted'] else 'НЕ принят'}")
        if self.kompas is not None and out_m3d:
            result.update(self.builder._build_in_kompas(groups, out_m3d))
        return result
