"""日程生成读取生理期的接口（cycle_block）。"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

from astrbot_plugin_busy_schedule.core.generator import ScheduleGenerator


def _generator(context, config=None) -> ScheduleGenerator:
    gen = object.__new__(ScheduleGenerator)
    gen.context = context
    gen.config = config or {}
    return gen


def test_missing_callback_returns_empty():
    gen = _generator(SimpleNamespace())
    assert asyncio.run(gen._get_cycle_context("umo", date(2026, 11, 5))) == ""


def test_unavailable_returns_empty():
    async def callback(umo, target, level):
        return {"available": False, "line": "", "phase": "", "day": 0}

    gen = _generator(SimpleNamespace(_emotion_state_cycle_context=callback))
    assert asyncio.run(gen._get_cycle_context("umo", date(2026, 11, 5))) == ""


def test_line_becomes_full_block_with_constraint():
    async def callback(umo, target, level):
        return {"available": True, "line": "经期第2天，容易累", "phase": "period", "day": 2}

    gen = _generator(SimpleNamespace(_emotion_state_cycle_context=callback))
    block = asyncio.run(gen._get_cycle_context("umo", date(2026, 11, 5)))
    assert "## 今日身体状态（软参考）" in block
    assert "经期第2天，容易累" in block
    # 约束句必须在：日程文本里不能出现「经期第几天」，否则会绕回对话注入。
    assert "不要在日程文本里写" in block


def test_chinese_level_is_translated_for_the_callback():
    seen = {}

    async def callback(umo, target, level):
        seen["level"] = level
        return {"available": True, "line": "经期第2天", "phase": "period", "day": 2}

    gen = _generator(
        SimpleNamespace(_emotion_state_cycle_context=callback),
        config={"cycle_schedule_level": "重"},
    )
    asyncio.run(gen._get_cycle_context("umo", date(2026, 11, 5)))
    assert seen["level"] == "heavy"


def test_callback_exception_is_swallowed():
    async def callback(umo, target, level):
        raise RuntimeError("对面炸了")

    gen = _generator(SimpleNamespace(_emotion_state_cycle_context=callback))
    assert asyncio.run(gen._get_cycle_context("umo", date(2026, 11, 5))) == ""


def test_prompt_falls_back_when_template_has_no_placeholder():
    """用户模板是自定义的：改默认模板对他们无效，兜底必须生效。"""
    generator = ScheduleGenerator.__new__(ScheduleGenerator)
    generator.config = {
        "日程生成": {
            "prompt_template": "{date_str}|{persona_desc}|{emotion_context}",
            "special_day_rules": [],
        }
    }

    async def text_stub(*_args, **_kwargs):
        return "stub"

    async def cycle_stub(*_args, **_kwargs):
        return {"available": True, "line": "经期第2天，容易累", "phase": "period", "day": 2}

    generator.context = SimpleNamespace(_emotion_state_cycle_context=cycle_stub)
    generator._get_persona_desc = text_stub
    generator._get_emotion_context = text_stub
    generator._get_attention_context = text_stub
    generator._get_recent_memories_context = text_stub
    generator._get_recent_chats = text_stub
    generator._get_rag_context = text_stub
    generator._get_history_schedules = lambda _target: "history"
    generator._get_yesterday_last_activity = lambda _target: "yesterday"

    prompt = asyncio.run(
        generator._build_prompt(
            date(2026, 11, 5),
            creative_context={
                "daily_theme": "theme",
                "mood_color": "mood",
                "outfit_style": "outfit",
                "schedule_type": "schedule",
            },
        )
    )
    assert "## 今日身体状态（软参考）" in prompt
    assert "经期第2天，容易累" in prompt
    assert "不要在日程文本里写" in prompt


def test_prompt_has_no_cycle_block_when_not_configured():
    generator = ScheduleGenerator.__new__(ScheduleGenerator)
    generator.config = {
        "日程生成": {
            "prompt_template": "{date_str}|{persona_desc}|{emotion_context}",
            "special_day_rules": [],
        }
    }

    async def text_stub(*_args, **_kwargs):
        return "stub"

    async def cycle_stub(*_args, **_kwargs):
        return {"available": False, "line": "", "phase": "", "day": 0}

    generator.context = SimpleNamespace(_emotion_state_cycle_context=cycle_stub)
    generator._get_persona_desc = text_stub
    generator._get_emotion_context = text_stub
    generator._get_attention_context = text_stub
    generator._get_recent_memories_context = text_stub
    generator._get_recent_chats = text_stub
    generator._get_rag_context = text_stub
    generator._get_history_schedules = lambda _target: "history"
    generator._get_yesterday_last_activity = lambda _target: "yesterday"

    prompt = asyncio.run(
        generator._build_prompt(
            date(2026, 11, 5),
            creative_context={
                "daily_theme": "theme",
                "mood_color": "mood",
                "outfit_style": "outfit",
                "schedule_type": "schedule",
            },
        )
    )
    assert "今日身体状态" not in prompt
