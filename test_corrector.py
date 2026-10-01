"""Tests for the background schedule corrector (v2.13)."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace

import astrbot_plugin_busy_schedule.main as main_module
from astrbot_plugin_busy_schedule.core.corrector import (
    CorrectionOutcome,
    ScheduleCorrector,
    parse_correction_times,
)
from astrbot_plugin_busy_schedule.core.data import (
    BusyPeriod,
    ScheduleData,
    ScheduleDataManager,
)
from astrbot_plugin_busy_schedule.main import BusySchedulePlugin

OWNER_DATE = date(2026, 10, 1)
SCHEDULE_TIME = (7, 0)
NOW = datetime(2026, 10, 1, 14, 0)


# ----------------------------------------------------------------------
# stubs
# ----------------------------------------------------------------------
class GeneratorStub:
    def __init__(self, reply: str = ""):
        self.reply = reply
        self.prompts: list[str] = []
        self.weather_service = None

    def _get_providers(self, primary_override=None):
        return ["stub-provider"]

    async def _call_llm(
        self, prompt, providers, session_id, max_retries=None, timeout_seconds=None
    ):
        self.prompts.append(prompt)
        return self.reply, providers[0]


def make_schedule():
    return ScheduleData(
        date="2026-10-01",
        outfit="白色连衣裙",
        hairstyle="双马尾",
        status="completed",
        busy_periods=[
            BusyPeriod(
                start_time="09:00", end_time="12:00", activity="在家自拍", is_busy=False
            ),
            BusyPeriod(
                start_time="12:00", end_time="13:00", activity="午餐", is_busy=False
            ),
            BusyPeriod(start_time="23:00", end_time=None, activity="睡觉"),
        ],
    )


def make_corrector(tmp_path, config=None, generator=None, after_apply=None):
    mgr = ScheduleDataManager(tmp_path / "schedule_data.json")
    mgr.set(OWNER_DATE, make_schedule())
    corrector = ScheduleCorrector(
        SimpleNamespace(),
        config or {},
        mgr,
        generator or GeneratorStub(""),
        after_apply=after_apply,
    )
    return corrector, mgr


def llm_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ----------------------------------------------------------------------
# core: ScheduleCorrector.run_correction
# ----------------------------------------------------------------------
def test_no_completed_schedule_skips(tmp_path):
    corrector = ScheduleCorrector(
        SimpleNamespace(), {}, ScheduleDataManager(tmp_path / "s.json"), GeneratorStub()
    )
    outcome = asyncio.run(corrector.run_correction(OWNER_DATE, "umo", now=NOW))

    assert outcome.triggered is False
    assert outcome.changed is False


def test_changed_false_leaves_data_untouched(tmp_path):
    refreshed = []

    async def after_apply():
        refreshed.append(True)

    corrector, mgr = make_corrector(
        tmp_path,
        generator=GeneratorStub(
            llm_json({"changed": False, "reason": "一切正常", "operations": []})
        ),
        after_apply=after_apply,
    )
    before = mgr.get(OWNER_DATE).schedule

    outcome = asyncio.run(corrector.run_correction(OWNER_DATE, "umo", now=NOW))

    assert outcome.triggered is True
    assert outcome.changed is False
    assert outcome.reason == "一切正常"
    assert mgr.get(OWNER_DATE).schedule == before
    assert refreshed == []


def test_applies_valid_op_and_skips_invalid_op(tmp_path):
    refreshed = []

    async def after_apply():
        refreshed.append(True)

    payload = {
        "changed": True,
        "reason": "用户下午想拍照",
        "operations": [
            {
                "action": "add",
                "start_time": "15:00",
                "end_time": "16:00",
                "activity": "小怡在天台拍照【外出】",
                "is_busy": False,
            },
            {"action": "remove", "target_start_time": "23:00"},
        ],
    }
    corrector, mgr = make_corrector(
        tmp_path, generator=GeneratorStub(llm_json(payload)), after_apply=after_apply
    )

    outcome = asyncio.run(corrector.run_correction(OWNER_DATE, "umo", now=NOW))

    assert outcome.triggered is True
    assert outcome.changed is True
    assert outcome.applied == 1
    assert outcome.skipped == 1
    data = mgr.get(OWNER_DATE)
    assert any("天台拍照" in p.activity for p in data.busy_periods)
    assert any(p.is_sleep for p in data.busy_periods)
    assert refreshed == [True]
    assert any("天台拍照" in change for change in outcome.changes)


def test_important_removal_needs_confirmation_skipped(tmp_path):
    payload = {
        "changed": True,
        "reason": "想取消午餐",
        "operations": [{"action": "remove", "target_start_time": "12:00"}],
    }
    corrector, mgr = make_corrector(
        tmp_path, generator=GeneratorStub(llm_json(payload))
    )
    before = mgr.get(OWNER_DATE).schedule

    outcome = asyncio.run(corrector.run_correction(OWNER_DATE, "umo", now=NOW))

    assert outcome.triggered is True
    assert outcome.changed is False
    assert outcome.applied == 0
    assert outcome.skipped == 1
    assert "保持原样" in outcome.note
    assert mgr.get(OWNER_DATE).schedule == before


def test_all_ops_rejected_keeps_schedule(tmp_path):
    payload = {
        "changed": True,
        "reason": "乱来的操作",
        "operations": [
            {"action": "remove", "target_start_time": "12:00"},
            {"action": "remove", "target_start_time": "09:00"},
        ],
    }
    corrector, mgr = make_corrector(
        tmp_path, generator=GeneratorStub(llm_json(payload))
    )

    outcome = asyncio.run(corrector.run_correction(OWNER_DATE, "umo", now=NOW))

    assert outcome.changed is False
    assert outcome.skipped == 2
    assert outcome.applied == 0
    assert "保持原样" in outcome.note


def test_llm_failure_returns_triggered_note(tmp_path):
    class BoomGenerator(GeneratorStub):
        async def _call_llm(self, *args, **kwargs):
            raise RuntimeError("provider down")

    corrector, _mgr = make_corrector(tmp_path, generator=BoomGenerator())

    outcome = asyncio.run(corrector.run_correction(OWNER_DATE, "umo", now=NOW))

    assert outcome.triggered is True
    assert outcome.changed is False
    assert "模型调用失败" in outcome.note


def test_template_unknown_placeholder_tolerated(tmp_path):
    config = {
        "日程修正": {
            "correction_prompt_template": (
                "时间 {current_time} 穿搭 {outfit} 发型行[{hairstyle_line}] "
                "神秘 {no_such_key} 剩余[{remaining_schedule}]"
            )
        }
    }
    corrector, _mgr = make_corrector(tmp_path, config=config)

    prompt, ctx = asyncio.run(
        corrector._build_context(OWNER_DATE, SCHEDULE_TIME, "", "", NOW)
    )

    assert NOW.strftime("%H:%M") in prompt
    assert "白色连衣裙" in prompt
    assert "双马尾" in prompt  # hairstyle_line was substituted
    assert "{no_such_key}" in prompt  # unknown placeholder kept verbatim
    assert "睡觉" in prompt  # at 14:00 only sleep remains in the day
    assert ctx["emotion_context"]  # default text present


def test_parse_correction_times_normalizes_and_dedups():
    assert parse_correction_times(["9:5", "12:00", "9:05", "bad", "", 25, None]) == [
        "09:05",
        "12:00",
    ]
    assert parse_correction_times("8:30") == ["08:30"]
    assert parse_correction_times([]) == []


def test_correction_outcome_summary_shapes():
    assert "未触发" in CorrectionOutcome(triggered=False, note="没有日程").summary()
    assert "日程未变" in CorrectionOutcome(
        triggered=True, changed=False, reason="一切正常"
    ).summary()
    text = CorrectionOutcome(
        triggered=True,
        changed=True,
        applied=2,
        skipped=1,
        reason="用户约定",
        changes=("a", "b"),
    ).summary()
    assert "应用 2 项" in text and "跳过 1 项" in text and "a" in text


# ----------------------------------------------------------------------
# main: trigger logic in _maybe_trigger_correction
# ----------------------------------------------------------------------
def _patch_now(monkeypatch, hour: int, minute: int):
    class FakeDateTime(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 10, 1, hour, minute)

    monkeypatch.setattr(main_module, "datetime", FakeDateTime)


def _make_plugin(config, *, sleeping: bool = False) -> BusySchedulePlugin:
    plugin = object.__new__(BusySchedulePlugin)
    plugin.config = config
    plugin.corrector = SimpleNamespace()
    plugin.generator = SimpleNamespace()
    plugin._correction_task = None
    plugin._corrections_done = {}
    plugin._correction_history = []
    plugin._last_context_time = ""
    plugin._state_file = None  # _save_state becomes a no-op
    period = (
        BusyPeriod(start_time="23:00", end_time=None, activity="睡觉")
        if sleeping
        else None
    )
    plugin.busy_mgr = SimpleNamespace(is_busy=sleeping, _current_busy_period=period)
    return plugin


def _correction_config(times: list[str]) -> dict:
    return {"日程修正": {"correction_enabled": True, "correction_times": times}}


def test_trigger_disabled_does_nothing(monkeypatch):
    plugin = _make_plugin({"日程修正": {"correction_enabled": False, "correction_times": ["12:00"]}})
    _patch_now(monkeypatch, 14, 0)

    plugin._maybe_trigger_correction()

    assert plugin._correction_task is None
    assert plugin._corrections_done == {}


def test_trigger_fires_due_time_and_dedups(monkeypatch):
    plugin = _make_plugin(_correction_config(["12:00", "15:00"]))
    fired: list[str] = []

    async def fake_run(time_point, umo=None):
        fired.append(time_point)
        return None

    monkeypatch.setattr(plugin, "_run_correction", fake_run)
    _patch_now(monkeypatch, 14, 0)

    created = []
    real_create = asyncio.create_task

    def capture(coro, **kwargs):
        created.append(coro)
        return real_create(coro, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", capture)

    async def drive():
        plugin._maybe_trigger_correction()  # 14:00 → 12:00 due, 15:00 not yet
        if created:
            await created[0]
        plugin._maybe_trigger_correction()  # 12:00 done, 15:00 still future

    asyncio.run(drive())

    assert fired == ["12:00"]
    assert plugin._corrections_done["2026-10-01"] == ["12:00"]
    assert plugin._correction_task is not None


def test_trigger_skips_schedule_time_point(monkeypatch):
    plugin = _make_plugin(_correction_config(["07:00", "12:00"]))
    fired: list[str] = []

    async def fake_run(time_point, umo=None):
        fired.append(time_point)
        return None

    monkeypatch.setattr(plugin, "_run_correction", fake_run)
    _patch_now(monkeypatch, 14, 0)

    created = []
    real_create = asyncio.create_task

    def capture(coro, **kwargs):
        created.append(coro)
        return real_create(coro, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", capture)

    async def drive():
        plugin._maybe_trigger_correction()  # 07:00 == schedule_time → mark only
        plugin._maybe_trigger_correction()  # 12:00 due → fire

    asyncio.run(drive())

    assert fired == ["12:00"]
    assert plugin._corrections_done["2026-10-01"] == ["07:00", "12:00"]


def test_trigger_skips_when_sleeping(monkeypatch):
    plugin = _make_plugin(_correction_config(["02:00"]), sleeping=True)
    fired: list[str] = []

    async def fake_run(time_point, umo=None):
        fired.append(time_point)
        return None

    monkeypatch.setattr(plugin, "_run_correction", fake_run)
    _patch_now(monkeypatch, 2, 5)

    plugin._maybe_trigger_correction()

    assert fired == []
    assert plugin._corrections_done["2026-10-01"] == ["02:00"]


def test_trigger_not_yet_due(monkeypatch):
    plugin = _make_plugin(_correction_config(["15:00"]))
    _patch_now(monkeypatch, 14, 0)

    plugin._maybe_trigger_correction()

    assert plugin._correction_task is None
    assert "15:00" not in plugin._corrections_done.get("2026-10-01", [])


def test_trigger_resets_next_day(monkeypatch):
    plugin = _make_plugin(_correction_config(["12:00"]))
    plugin._corrections_done = {"2026-09-30": ["12:00"]}
    fired: list[str] = []

    async def fake_run(time_point, umo=None):
        fired.append(time_point)
        return None

    monkeypatch.setattr(plugin, "_run_correction", fake_run)
    _patch_now(monkeypatch, 14, 0)

    created = []
    real_create = asyncio.create_task

    def capture(coro, **kwargs):
        created.append(coro)
        return real_create(coro, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", capture)

    async def drive():
        plugin._maybe_trigger_correction()
        if created:
            await created[0]

    asyncio.run(drive())

    assert fired == ["12:00"]
    assert "2026-10-01" in plugin._corrections_done
