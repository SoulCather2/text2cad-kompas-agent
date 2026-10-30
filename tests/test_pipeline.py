"""Голосование, исправление ошибок, обезличивание — без сети. Запуск: python -m tests.test_pipeline"""
import json
from agent.llm import ReplayLLM
from agent.privacy import Privacy, PrivacyError
from agent.solver import Solver

cfg = {"llm": {"n_samples": 5, "parallel": 1, "max_repair_rounds": 2}}
gold = {"bbox_expected": [60, 40, 3], "ops": [{"op": "extrude", "sketch": [{"type": "rect", "w": 60, "h": 40}], "depth": 3},
                                              {"op": "holes", "d": 4, "points": [[0, 0]]}]}
wrong = {"bbox_expected": [60, 40, 3], "ops": [{"op": "extrude", "sketch": [{"type": "rect", "w": 60, "h": 40}], "depth": 3}]}
G, W = json.dumps(gold), json.dumps(wrong)

# 1) голосование: 3 правильных против 1 неверного и 1 сломанного
r = Solver(ReplayLLM({"a": ["{not json", W, G, G, G]}), cfg).solve("пластина 60x40x3, отверстие 4", "a")
assert r["status"] == "ok" and r["groups"][0]["size"] == 3 and len(r["spec"]["ops"]) == 2, r["groups"]
print("голосование OK:", r["groups"])

# 2) все ответы сломаны → запрос на исправление с текстом ошибки → правильный
bad = json.dumps({"ops": [{"op": "extrude", "sketch": [{"type": "rect", "w": 60}], "depth": 3}]})
llm = ReplayLLM({"b": [bad] * 5 + [G]})
r = Solver(llm, cfg).solve("пластина", "b")
assert r["status"] == "ok" and r["n_candidates"] == 6, r
print("исправление OK, ошибки модели:", r["errors"][0])

# 3) габарит не совпал с ожидаемым самой моделью → повторный запрос
mism = json.dumps({**wrong, "bbox_expected": [60, 40, 5]})
r = Solver(ReplayLLM({"c": [mism, mism, G, G, G, G]}), cfg).solve("пластина", "c")
print("самопроверка габарита OK:", r["groups"])

# 4) обезличивание
p = Privacy(["альфапроект", "бетазавод"], "деталь")
s, hits = p.clean("Крепление Альфапроекта для бетазавода: пластина 60×40×3")
assert "льфапроек" not in s and "етазаво" not in s, s
print("обезличивание OK:", s, hits)
try:
    Privacy(["нии"], mode="block").clean("для НИИ"); raise SystemExit("block не сработал")
except PrivacyError:
    print("блокировка OK")
print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ")
