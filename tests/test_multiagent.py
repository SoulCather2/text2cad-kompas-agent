"""Мультиагентная система без сети (подставная модель). Запуск: python -m tests.test_multiagent"""
import json

from agent.llm import ReplayLLM
from agent.multiagent import MultiAgent, summarize

cfg = {"llm": {"n_samples": 5, "parallel": 1, "max_repair_rounds": 2}, "multiagent": {"max_rounds": 3}}
TEXT = "Пластина 60×40×3 мм, в центре сквозное отверстие диаметром 4 мм."
gold = {"bbox_expected": [60, 40, 3], "ops": [{"op": "extrude", "sketch": [{"type": "rect", "w": 60, "h": 40}], "depth": 3},
                                              {"op": "holes", "d": 4, "points": [[0, 0]]}]}
wrong = {"bbox_expected": [60, 40, 3], "ops": [{"op": "extrude", "sketch": [{"type": "rect", "w": 60, "h": 40}], "depth": 3}]}
G, W = json.dumps(gold), json.dumps(wrong)
AN_OK = json.dumps({"issues": [], "assumptions": ["отверстие сквозное по толщине 3 мм"],
                    "clarified_task": "1. Пластина 60×40×3 мм.\n2. Сквозное отверстие диаметром 4 мм в центре пластины.",
                    "checklist": ["пластина 60×40×3", "1 сквозное отверстие Ø4 в центре"], "bbox_expected": [60, 40, 3]},
                   ensure_ascii=False)
AN_LOST = json.dumps({"issues": [], "assumptions": [], "clarified_task": "Пластина 60×40×3 мм с отверстием в центре.",
                      "checklist": [], "bbox_expected": [60, 40, 3]}, ensure_ascii=False)
REDO = json.dumps({"verdict": "redo", "problems": ["нет отверстия Ø4 в центре — добавь holes d=4 в (0;0)"],
                   "fix_target": "designer"}, ensure_ascii=False)
ACC = json.dumps({"verdict": "accept", "problems": [], "fix_target": "designer"})

# 1) контролёр находит ошибку → конструктор исправляет во втором раунде
llm = ReplayLLM({"a:аналитик": [AN_OK], "a:конструктор": [W] * 5 + [G] * 5, "a:контролёр": [REDO, ACC]})
r = MultiAgent(llm, cfg).solve(TEXT, "a")
assert r["status"] == "ok" and r["accepted"] and r["chosen_round"] == 2, r
assert abs(r["ref"]["volume"] - (7200 - 3.14159265 * 4 * 3)) < 1, r["ref"]
print("1) обратная связь контролёр → конструктор OK:", [t["agent"] for t in r["agents"]])

# 2) аналитик потерял число 4 → программа возвращает ему ответ → исправлено
llm = ReplayLLM({"b:аналитик": [AN_LOST, AN_OK], "b:конструктор": [G] * 5, "b:контролёр": [ACC]})
r = MultiAgent(llm, cfg).solve(TEXT, "b")
assert r["status"] == "ok" and r["accepted"] and llm.calls["b:аналитик"] == 2 and "4 мм" in r["clarified_task"], r
print("2) проверка аналитика (потерянные числа) OK")

# 3) контролёр не принял ни разу → выбирается вариант с наименьшим числом замечаний (более ранний при равенстве)
llm = ReplayLLM({"c:аналитик": [AN_OK], "c:конструктор": [G] * 15, "c:контролёр": [REDO]})
r = MultiAgent(llm, cfg).solve(TEXT, "c")
assert r["status"] == "ok" and not r["accepted"] and r["chosen_round"] == 1 and llm.calls["c:контролёр"] == 3, r
print("3) нет принятых → откат к лучшему OK, предупреждение:", r["warning"])

# 4) контролёр отвечает мусором → решение по фактам (габарит совпал с ожидаемым аналитиком)
llm = ReplayLLM({"d:аналитик": [AN_OK], "d:конструктор": [G] * 5, "d:контролёр": ["{сломано"]})
r = MultiAgent(llm, cfg).solve(TEXT, "d")
assert r["status"] == "ok" and r["accepted"], r
print("4) контролёр недоступен → проверка по фактам OK")

# 5) аналитик сломан → исходный текст, конструктор работает как раньше
llm = ReplayLLM({"e:аналитик": ["нет json"], "e:конструктор": [G] * 5, "e:контролёр": [ACC]})
r = MultiAgent(llm, cfg).solve(TEXT, "e")
assert r["status"] == "ok" and r["clarified_task"] == TEXT, r
print("5) аналитик недоступен → исходное задание OK")

# 6) контролёр: «неверно понято задание» → аналитик пересматривает → конструктор заново
REDO_A = json.dumps({"verdict": "redo", "problems": ["отверстие должно быть сквозным"], "fix_target": "analyst"}, ensure_ascii=False)
llm = ReplayLLM({"f:аналитик": [AN_OK, AN_OK], "f:конструктор": [G] * 10, "f:контролёр": [REDO_A, ACC]})
r = MultiAgent(llm, cfg).solve(TEXT, "f")
assert r["accepted"] and llm.calls["f:аналитик"] == 2 and r["chosen_round"] == 2, r
print("6) обратная связь контролёр → аналитик OK")

# 7) строгий режим: задание без диаметра отверстия → отклонено, вопросы заказчику
REJ = json.dumps({"verdict": "reject", "issues": ["не указан диаметр отверстия"], "questions": ["Какой диаметр отверстия?"],
                  "assumptions": [], "clarified_task": "Пластина 60×40×3 мм, отверстие в центре.", "checklist": []}, ensure_ascii=False)
T2 = "Пластина 60×40×3 мм, в центре отверстие."
llm = ReplayLLM({"g:аналитик": [REJ]})
r = MultiAgent(llm, cfg).solve(T2, "g")
assert r["status"] == "rejected" and r["questions"] == ["Какой диаметр отверстия?"] and "g:конструктор" not in llm.calls, r
print("7) отклонение задания OK:", r["errors"][0])

# 8) отклонено → заказчик ответил в консоли → аналитик принял → деталь построена
asked = []
def ask(qs, issues):
    asked.append(qs)
    return [(qs[0], "4 мм, сквозное")]
AN_OK2 = json.loads(AN_OK); AN_OK2["clarified_task"] += " (по уточнению заказчика)"
llm = ReplayLLM({"h:аналитик": [REJ, json.dumps(AN_OK2, ensure_ascii=False)], "h:конструктор": [G] * 5, "h:контролёр": [ACC]})
r = MultiAgent(llm, cfg, ask=ask).solve(T2, "h")
assert r["status"] == "ok" and asked and "Уточнения заказчика" in r["task_text_final"] and "4 мм, сквозное" in r["task_text_final"], r
print("8) вопрос → ответ заказчика → построено OK")

# 9) программная проверка: отверстие мимо материала → «операция без эффекта» → переделка
miss = {"bbox_expected": [60, 40, 3], "ops": [{"op": "extrude", "sketch": [{"type": "rect", "w": 60, "h": 40}], "depth": 3},
                                              {"op": "holes", "d": 4, "points": [[100, 0]]}]}
cfg2 = {**cfg, "multiagent": {"max_rounds": 2, "inspector": False}}
llm = ReplayLLM({"i:аналитик": [AN_OK], "i:конструктор": [json.dumps(miss)] * 5 + [G] * 5})
r = MultiAgent(llm, cfg2).solve(TEXT, "i")
first = next(t for t in r["agents"] if t["agent"] == "контролёр")
assert first["verdict"] == "redo" and "пустоту" in first["problems"][0] and r["accepted"] and r["chosen_round"] == 2, r["agents"]
print("9) вырез мимо материала пойман программно OK:", first["problems"][0])

print("сводка элементов:", r["agents"][-1]["facts"]["элементы"])
print("ВСЕ ТЕСТЫ МУЛЬТИАГЕНТА ПРОЙДЕНЫ")
