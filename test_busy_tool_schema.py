from __future__ import annotations

from pathlib import Path

import docstring_parser

from astrbot_plugin_busy_schedule.main import BusySchedulePlugin


def test_schedule_tool_description_is_layered_for_skills_like() -> None:
    parsed = docstring_parser.parse(BusySchedulePlugin.edit_current_schedule.__doc__ or "")
    assert parsed.description is not None
    assert len(parsed.description.strip()) < 150
    assert "不能仅因日程含天气描述" in parsed.description
    assert "set_outfit" not in parsed.description

    parameters = {item.arg_name: item.description or "" for item in parsed.params}
    assert "set_outfit" in parameters["operations_json"]
    assert "当前普通活动只能改 end_time" in parameters["operations_json"]
    assert "同一数组中同步更新" in parameters["operations_json"]
    assert "重要活动" in parameters["mode"]
    assert "已经确认" in parameters["confirmed_important"]


def test_schedule_tool_version_is_current() -> None:
    root = Path(__file__).parent
    metadata = (root / "metadata.yaml").read_text(encoding="utf-8")
    source = (root / "main.py").read_text(encoding="utf-8")
    assert "version: v2.12.1" in metadata
    assert '"v2.12.1"' in source
