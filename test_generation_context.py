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


# ----------------------------------------------------------------------
# v2.14.12 recent chats: LivingMemory full store preferred
# ----------------------------------------------------------------------
def test_recent_chats_prefers_livingmemory_store():
    captured = {}

    async def history(session_id, since, limit):
        captured["args"] = (session_id, since, limit)
        return [
            {
                "speaker": "user",
                "name": "登登",
                "at": "2026-10-11T17:55:17+00:00",
                "text": "明天你可以做一顿丰盛的大餐不你替我吃",
            },
            {
                "speaker": "assistant",
                "name": "小怡",
                "at": "2026-10-11T17:56:00+00:00",
                "text": "好呀，明天给你安排上",
            },
            "not-a-dict",
            {"speaker": "user", "text": "   "},
        ]

    gen = _generator(SimpleNamespace(_livingmemory_get_attention_history=history))
    text = asyncio.run(gen._get_recent_chats(UMO, rounds=5))

    assert captured["args"] == (UMO, "", 10)  # rounds*2 条
    lines = text.splitlines()
    assert len(lines) == 2
    # 事故原话 + 本地时间戳（UTC 17:55 = 北京 01:55，跨天场景）
    assert lines[0] == "[10-12 01:55] 用户: 明天你可以做一顿丰盛的大餐不你替我吃"
    assert lines[1].startswith("[10-12 01:56] 我: ")


def test_recent_chats_falls_back_without_livingmemory():
    called = {}

    async def conv(umo, rounds, include_datetime=False):
        called["rounds"] = rounds
        return [{"role": "user", "content": "旧窗口消息"}]

    # 回调缺失
    bare = _generator(SimpleNamespace())
    bare._get_conversation_contexts = conv
    out = asyncio.run(bare._get_recent_chats(UMO, rounds=10))
    assert "旧窗口消息" in out
    assert called["rounds"] == 10

    # 回调抛异常
    async def boom(*args):
        raise RuntimeError("boom")

    failed = _generator(SimpleNamespace(_livingmemory_get_attention_history=boom))
    failed._get_conversation_contexts = conv
    assert "旧窗口消息" in asyncio.run(failed._get_recent_chats(UMO, rounds=3))

    # 回调返回空
    async def empty(*args):
        return []

    hollow = _generator(SimpleNamespace(_livingmemory_get_attention_history=empty))
    hollow._get_conversation_contexts = conv
    assert "旧窗口消息" in asyncio.run(hollow._get_recent_chats(UMO, rounds=2))


def test_local_message_stamp_naive_and_junk():
    aware = ScheduleGenerator._local_message_stamp("2026-10-11T17:55:17+00:00")
    naive = ScheduleGenerator._local_message_stamp("2026-10-11T17:55:17")
    assert aware == naive == "10-12 01:55"
    assert ScheduleGenerator._local_message_stamp("") == ""
    assert ScheduleGenerator._local_message_stamp("乱写") == ""
