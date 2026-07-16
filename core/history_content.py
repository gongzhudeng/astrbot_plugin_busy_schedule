from __future__ import annotations

import re
from typing import Any

_DATETIME_REMINDER_RE = re.compile(
    r"^<system_reminder>(?:[^\n]*\n)*Current datetime: "
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \([^)]+\)"
    r"(?:\n[^\n]*)*</system_reminder>$"
)


def _text_from_part(part: Any) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        return str(part.get("text") or part.get("content") or part.get("value") or "")
    return str(getattr(part, "text", "") or getattr(part, "content", "") or "")


def _is_datetime_reminder(text: str) -> bool:
    return bool(_DATETIME_REMINDER_RE.fullmatch(str(text or "").strip()))


def extract_history_text(content: Any, *, exclude_datetime: bool = False) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            text = _text_from_part(part).strip()
            if text and not (exclude_datetime and _is_datetime_reminder(text)):
                parts.append(text)
        return " ".join(parts).strip()
    return _text_from_part(content).strip()


def find_datetime_reminder(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    for part in content:
        text = _text_from_part(part).strip()
        if _is_datetime_reminder(text):
            return text
    return ""


def extract_semantic_history_text(content: Any) -> str:
    return extract_history_text(content, exclude_datetime=True)
