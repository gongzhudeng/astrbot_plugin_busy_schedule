"""Tests for generation-side attention/memory context (v2.14)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrbot_plugin_busy_schedule.core.generator import ScheduleGenerator

UMO = "private:test"


def _generator(context, config=None) -> ScheduleGenerator:
    gen = object.__new__(ScheduleGenerator)
    gen.context = context
    gen.config = config or {}
    return gen


def test_attention_context_from_callback():
    async def live(umo, limit):
        assert umo == UMO
        assert limit == 5
        return {"mood": "当前心情：开心。", "attention": "- [约定；仍待关注] 天台拍照"}

    gen = _generator(SimpleNamespace(_emotion_state_get_live_context=live))
    assert asyncio.run(gen._get_attention_context(UMO)) == "- [约定；仍待关注] 天台拍照"


def test_attention_context_fallbacks():
    calls: list[str] = []

    async def live(umo, limit):
        calls.append(umo)
        raise RuntimeError("boom")

    gen = _generator(SimpleNamespace(_emotion_state_get_live_context=live))
    assert asyncio.run(gen._get_attention_context(UMO)) == "暂无待关注事项。"
    assert calls == [UMO]

    empty = _generator(
        SimpleNamespace(
            _emotion_state_get_live_context=lambda *a: asyncio.sleep(
                0, {"mood": "x", "attention": ""}
            )
        )
    )
    assert asyncio.run(empty._get_attention_context(UMO)) == "暂无待关注事项。"

    bare = _generator(SimpleNamespace())
    assert asyncio.run(bare._get_attention_context(UMO)) == "暂无待关注事项。"
    assert asyncio.run(bare._get_attention_context(None)) == "暂无待关注事项。"


def test_recent_memories_format_and_truncation():
    captured = {}

    async def recent(session_id, since, limit):
        captured["args"] = (session_id, since, limit)
        return [
            {"time": "2026-10-01 00:20", "text": "x" * 600},
            {"time": "", "text": "短记忆"},
            {"text": "  "},
            "not-a-dict",
        ]

    gen = _generator(
        SimpleNamespace(_livingmemory_get_recent_memories=recent),
        {"日程生成": {"generation_memory_max_items": 2, "generation_memory_max_chars": 500}},
    )
    text = asyncio.run(gen._get_recent_memories_context(UMO))

    assert captured["args"] == (UMO, "", 2)
    lines = text.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("- [2026-10-01 00:20] ")
    assert len(lines[0]) == len("- [2026-10-01 00:20] ") + 500  # 截到 500
    assert lines[1] == "- [] 短记忆"


def test_recent_memories_zero_limit_skips_callback():
    async def recent(session_id, since, limit):  # pragma: no cover
        raise AssertionError("should not be called")

    gen = _generator(
        SimpleNamespace(_livingmemory_get_recent_memories=recent),
        {"日程生成": {"generation_memory_max_items": 0}},
    )
    assert asyncio.run(gen._get_recent_memories_context(UMO)) == "暂无近期记忆。"


def test_recent_memories_fallbacks():
    async def boom(*args):
        raise RuntimeError("boom")

    gen = _generator(SimpleNamespace(_livingmemory_get_recent_memories=boom))
    assert asyncio.run(gen._get_recent_memories_context(UMO)) == "暂无近期记忆。"

    empty = _generator(
        SimpleNamespace(_livingmemory_get_recent_memories=lambda *a: asyncio.sleep(0, []))
    )
    assert asyncio.run(empty._get_recent_memories_context(UMO)) == "暂无近期记忆。"

    bare = _generator(SimpleNamespace())
    assert asyncio.run(bare._get_recent_memories_context(None)) == "暂无近期记忆。"
