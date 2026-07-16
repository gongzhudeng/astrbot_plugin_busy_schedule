import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).parent / "core" / "history_content.py"
_SPEC = importlib.util.spec_from_file_location("busy_history_content", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
extract_history_text = _MODULE.extract_history_text
extract_semantic_history_text = _MODULE.extract_semantic_history_text
find_datetime_reminder = _MODULE.find_datetime_reminder


REMINDER = "<system_reminder>Current datetime: 2026-07-16 21:30 (CST)</system_reminder>"


def test_schedule_model_context_keeps_datetime():
    content = [
        {"type": "text", "text": "明天中午吃火锅"},
        {"type": "text", "text": REMINDER},
    ]

    assert extract_history_text(content) == f"明天中午吃火锅 {REMINDER}"
    assert find_datetime_reminder(content) == REMINDER


def test_schedule_retrieval_uses_chat_text_only():
    content = [
        {"type": "text", "text": "明天中午吃火锅"},
        {"type": "text", "text": REMINDER},
    ]

    assert extract_semantic_history_text(content) == "明天中午吃火锅"


def test_schedule_keeps_plain_user_text_unchanged():
    text = f"这不是独立部件：{REMINDER}"

    assert extract_semantic_history_text(text) == text
