from types import SimpleNamespace

from astrbot_plugin_busy_schedule.main import BusySchedulePlugin


def test_export_facts_includes_outfit_and_hairstyle() -> None:
    plugin = BusySchedulePlugin.__new__(BusySchedulePlugin)
    active = SimpleNamespace(
        owner_date=SimpleNamespace(isoformat=lambda: "2026-09-01"),
        data=SimpleNamespace(
            outfit="上装：白色衬衫\n下装：蓝色短裙",
            hairstyle="高马尾",
            weather=None,
        ),
    )
    plugin._get_active_schedule = lambda _now: active
    plugin._get_resolved_timeline = lambda _date: []
    plugin._export_timeline = lambda _date: []
    plugin.injector = SimpleNamespace(
        _get_activity_state=lambda _resolved, _now: (None, None)
    )
    plugin.busy_mgr = SimpleNamespace(is_busy=False)

    facts = plugin._export_facts()

    assert facts["outfit"] == "上装：白色衬衫\n下装：蓝色短裙"
    assert facts["hairstyle"] == "高马尾"


def test_export_facts_has_empty_outfit_without_active_schedule() -> None:
    plugin = BusySchedulePlugin.__new__(BusySchedulePlugin)
    plugin._get_active_schedule = lambda _now: None

    facts = plugin._export_facts()

    assert facts["outfit"] == ""
    assert facts["hairstyle"] == ""
