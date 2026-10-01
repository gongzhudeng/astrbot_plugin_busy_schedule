import importlib
import sys
from datetime import date, datetime
from types import ModuleType


class LoggerStub:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


if "astrbot.api" not in sys.modules:
    astrbot_module = ModuleType("astrbot")
    astrbot_api_module = ModuleType("astrbot.api")
    astrbot_api_module.logger = LoggerStub()
    astrbot_module.api = astrbot_api_module
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = astrbot_api_module

data_module = importlib.import_module("core.data")
schedule_editor_module = importlib.import_module("core.schedule_editor")

BusyPeriod = data_module.BusyPeriod
ScheduleData = data_module.ScheduleData
ScheduleEditor = schedule_editor_module.ScheduleEditor

OWNER_DATE = date(2026, 8, 25)
SCHEDULE_TIME = (7, 0)
NOW = datetime(2026, 8, 25, 19, 40)


def make_schedule(hairstyle="双马尾"):
    return ScheduleData(
        date="2026-08-25",
        outfit="小裙子",
        hairstyle=hairstyle,
        status="completed",
        busy_periods=[
            BusyPeriod(
                start_time="09:00",
                end_time="12:00",
                activity="在镜子面前自拍小裙子",
                is_busy=False,
            ),
            BusyPeriod(start_time="23:00", end_time=None, activity="睡觉"),
        ],
    )


def apply_outfit(operation, hairstyle="双马尾"):
    return ScheduleEditor().apply(
        make_schedule(hairstyle),
        [operation],
        owner_date=OWNER_DATE,
        schedule_time=SCHEDULE_TIME,
        now=NOW,
    )


def test_set_outfit_without_hairstyle_keeps_hairstyle():
    result = apply_outfit({"action": "set_outfit", "outfit": "蕾姆cosplay"})

    assert result.data.outfit == "蕾姆cosplay"
    assert result.data.hairstyle == "双马尾"
    assert "hairstyle unchanged: 双马尾" in result.changes
    assert "updated current outfit" in result.changes


def test_set_outfit_with_empty_hairstyle_clears_it():
    result = apply_outfit(
        {"action": "set_outfit", "outfit": "蕾姆cosplay", "hairstyle": ""}
    )

    assert result.data.outfit == "蕾姆cosplay"
    assert result.data.hairstyle == ""
    assert "cleared hairstyle" in result.changes


def test_set_outfit_with_new_hairstyle_updates_it():
    result = apply_outfit(
        {"action": "set_outfit", "outfit": "蕾姆cosplay", "hairstyle": "蓝色短发"}
    )

    assert result.data.hairstyle == "蓝色短发"
    assert "updated hairstyle to 蓝色短发" in result.changes


def test_set_outfit_with_same_hairstyle_reports_unchanged():
    result = apply_outfit(
        {"action": "set_outfit", "outfit": "蕾姆cosplay", "hairstyle": "双马尾"}
    )

    assert result.data.hairstyle == "双马尾"
    assert "hairstyle unchanged: 双马尾" in result.changes


def make_schedule_with_current_activity():
    return ScheduleData(
        date="2026-08-25",
        outfit="小裙子",
        hairstyle="双马尾",
        status="completed",
        busy_periods=[
            BusyPeriod(
                start_time="19:00",
                end_time="21:00",
                activity="在驿站取包裹",
                is_busy=False,
            ),
            BusyPeriod(start_time="23:00", end_time=None, activity="睡觉"),
        ],
    )


def apply_operation(operation, schedule=None):
    return ScheduleEditor().apply(
        schedule or make_schedule_with_current_activity(),
        [operation],
        owner_date=OWNER_DATE,
        schedule_time=SCHEDULE_TIME,
        now=NOW,
    )


def test_update_current_activity_description_only():
    result = apply_operation(
        {
            "action": "update",
            "target_start_time": "19:00",
            "activity": "先在街区逛拍街景和腿，最后顺路去驿站拿包裹",
        }
    )

    period = result.data.busy_periods[0]
    assert period.activity == "先在街区逛拍街景和腿，最后顺路去驿站拿包裹"
    assert period.start_time == "19:00"
    assert period.end_time == "21:00"


def test_update_current_activity_end_time_and_busy_flag():
    result = apply_operation(
        {
            "action": "update",
            "target_start_time": "19:00",
            "end_time": "22:00",
            "is_busy": True,
        }
    )

    period = result.data.busy_periods[0]
    assert period.end_time == "22:00"
    assert period.is_busy is True
    assert period.activity == "在驿站取包裹"


def test_update_current_activity_start_time_still_rejected():
    import pytest

    from core.schedule_editor import ScheduleEditConflict

    with pytest.raises(ScheduleEditConflict):
        apply_operation(
            {
                "action": "update",
                "target_start_time": "19:00",
                "start_time": "18:30",
            }
        )


def test_update_activity_strips_trailing_status_markers():
    result = apply_operation(
        {
            "action": "update",
            "target_start_time": "19:00",
            "activity": "在街区逛拍街景【外出】【忙碌】",
            "is_busy": True,
        }
    )

    period = result.data.busy_periods[0]
    assert period.activity == "在街区逛拍街景【外出】"
    assert "【忙碌】【忙碌】" not in result.data.schedule
    assert "在街区逛拍街景【外出】【忙碌】" in result.data.schedule


def test_add_activity_strips_trailing_status_markers():
    result = apply_operation(
        {
            "action": "add",
            "start_time": "21:00",
            "end_time": "22:00",
            "activity": "敷面膜刷手机【可回消息】",
        }
    )

    period = next(
        p for p in result.data.busy_periods if p.start_time == "21:00"
    )
    assert period.activity == "敷面膜刷手机"
    assert result.data.schedule.count("【可回消息】") == 2


def test_render_schedule_heals_dirty_activity_markers():
    dirty = make_schedule()
    dirty.busy_periods[0].activity = "自拍小裙子【可回消息】"

    text = schedule_editor_module._render_schedule(dirty.busy_periods)

    assert "【可回消息】【可回消息】" not in text
    assert text.count("【可回消息】") == 1
