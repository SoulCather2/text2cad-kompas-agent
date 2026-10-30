"""Клиент OpenAI-совместимого API (AITUNNEL, Yandex Cloud и др.) + журнал всех запросов."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from .privacy import Privacy

_lock = threading.Lock()


class RequestLog:
    """Журнал запросов: время, модель, отправленный текст, ответ, токены (JSON Lines)."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, rec: dict):
        with _lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class LLMClient:
    def __init__(self, cfg: dict, log: RequestLog, privacy: Privacy, model: str | None = None):
        from openai import OpenAI

        llm = cfg["llm"]
        prov = cfg["providers"][llm["provider"]]
        key = os.getenv(prov["api_key_env"])
        if not key:
            raise RuntimeError(f"нет ключа: задайте {prov['api_key_env']} в .env или переменных окружения")
        self.client = OpenAI(base_url=prov["base_url"], api_key=key,
                             timeout=llm.get("timeout", 90), max_retries=llm.get("max_retries", 2))
        self.model = model or llm["model"]
        self.json_mode = llm.get("json_mode", True)
        self.max_tokens = llm.get("max_tokens", 3000)
        self.log = log
        self.privacy = privacy

    def chat(self, messages: list[dict], temperature: float, task_id: str = "") -> str:
        messages, hits = self.privacy.clean_messages(messages)
        kw = dict(model=self.model, messages=messages, temperature=temperature, max_tokens=self.max_tokens)
        if self.json_mode:
            kw["response_format"] = {"type": "json_object"}
        t0 = time.time()
        rec = {"time": datetime.now().isoformat(timespec="seconds"), "task": task_id, "model": self.model,
               "temperature": temperature, "redacted": sorted(set(hits)), "request": messages}
        try:
            try:
                r = self.client.chat.completions.create(**kw)
            except Exception as e:  # модель может не поддерживать response_format
                if self.json_mode and "response_format" in str(e).lower():
                    kw.pop("response_format")
                    self.json_mode = False
                    r = self.client.chat.completions.create(**kw)
                else:
                    raise
            text = r.choices[0].message.content or ""
            fin = getattr(r.choices[0], "finish_reason", None)
            if fin == "length" and kw["max_tokens"] < 16000:
                # ответ обрезан (модель могла потратить лимит на рассуждения) — повторяем с большим лимитом
                kw["max_tokens"] = min(16000, kw["max_tokens"] * 2)
                r = self.client.chat.completions.create(**kw)
                text = r.choices[0].message.content or ""
                fin = getattr(r.choices[0], "finish_reason", None)
            rec["finish_reason"] = fin
            u = getattr(r, "usage", None)
            rec.update(response=text, latency_s=round(time.time() - t0, 2),
                       usage={"prompt": getattr(u, "prompt_tokens", None), "completion": getattr(u, "completion_tokens", None)})
            return text
        except Exception as e:
            rec.update(error=f"{type(e).__name__}: {e}", latency_s=round(time.time() - t0, 2))
            raise
        finally:
            self.log.write(rec)


class ReplayLLM:
    """Подставной клиент для тестов без сети: возвращает заранее заданные ответы."""

    def __init__(self, answers: dict[str, list[str]], log: RequestLog | None = None):
        self.answers = answers
        self.calls: dict[str, int] = {}
        self.model = "replay"
        self.log = log

    def chat(self, messages, temperature, task_id=""):
        lst = self.answers[task_id]
        i = self.calls.get(task_id, 0)
        self.calls[task_id] = i + 1
        text = lst[min(i, len(lst) - 1)]
        if self.log:
            self.log.write({"time": datetime.now().isoformat(timespec="seconds"), "task": task_id, "model": "replay",
                            "temperature": temperature, "request": messages, "response": text})
        return text
