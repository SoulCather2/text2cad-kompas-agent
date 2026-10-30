"""Защита данных: в модель ИИ не уходят названия изделия, проекта, организации и фамилии."""
from __future__ import annotations

import re
from pathlib import Path


class PrivacyError(RuntimeError):
    pass


class Privacy:
    def __init__(self, banned_terms: list[str], replacement: str = "деталь", mode: str = "redact"):
        """banned_terms — основы слов (регистр не важен): 'кронштейн' ловит все падежи.
        mode: 'redact' — заменить и отправить; 'block' — не отправлять вовсе."""
        self.terms = [t.strip().lower() for t in banned_terms if t.strip()]
        self.replacement = replacement
        self.mode = mode
        self.rx = re.compile(r"(?i)\b(" + "|".join(re.escape(t) for t in self.terms) + r")\w*") if self.terms else None

    @classmethod
    def from_config(cls, pv: dict, root: str | Path = ".") -> "Privacy":
        """Слова — из config.yaml (banned_terms) и из локального файла banned_terms_file
        (по одной основе в строке, # — комментарий). Файл не коммитится, чтобы список не утёк."""
        terms = list(pv.get("banned_terms") or [])
        f = Path(root) / pv.get("banned_terms_file", "banned_terms.txt")
        if f.exists():
            terms += [ln.split("#")[0].strip() for ln in f.read_text(encoding="utf-8").splitlines()]
        else:
            print(f"ВНИМАНИЕ: нет {f.name} — обезличивание без списка слов (см. banned_terms.example.txt)")
        return cls(terms, pv.get("replacement", "деталь"), pv.get("mode", "redact"))

    def find(self, text: str) -> list[str]:
        return [m.group(0) for m in self.rx.finditer(text)] if self.rx else []

    def clean(self, text: str) -> tuple[str, list[str]]:
        hits = self.find(text)
        if not hits:
            return text, []
        if self.mode == "block":
            raise PrivacyError(f"запрос содержит запрещённые слова: {sorted(set(hits))}")
        out = self.rx.sub(self.replacement, text)
        if self.find(out):
            raise PrivacyError("не удалось обезличить запрос")
        return out, hits

    def clean_messages(self, messages: list[dict]) -> tuple[list[dict], list[str]]:
        out, hits = [], []
        for m in messages:
            c, h = self.clean(m["content"])
            out.append({**m, "content": c})
            hits += h
        return out, hits
