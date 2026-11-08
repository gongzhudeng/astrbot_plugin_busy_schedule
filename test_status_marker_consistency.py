"""状态标记一致性：日程文本、运行时忙碌区间、日程图片必须同源。

回归背景（2026-11-08）：模型偶尔漏写行尾的【忙碌】/【可回消息】，
只留下【主动分享】这类分类标签。生成侧把“缺标记”当成忙碌、图片侧当成
可回复，导致 15:08 的活动在图上写“可回复”、实际却被拦截排队。
"""

from datetime import date, datetime

import pytest

from astrbot_plugin_busy_schedule.core.data import BusyPeriod
from astrbot_plugin_busy_schedule.core.generator import ScheduleGenerator
from astrbot_plugin_busy_schedule.core.image_renderer import BusyScheduleImageRenderer

# 复刻 2026-11-08 的真实片段：带分类标签但没有状态标记。
SCHEDULE = (
    "13:24-15:08 小怡窝在被子里翻着片子当消遣【忙碌】\n"
    "15:08-15:52 小怡在收集小游戏里摆房间，截图发给Mando【主动分享】\n"
    "15:52-16:29 小怡跟林雅琪微信闲聊，分享链接拼单【可回消息】\n"
    "16:29-17:33 小怡铺瑜伽垫拉伸，做了几组臀桥【忙碌】\n"
    "01:36 小怡睡觉【忙碌】"
)


def _generator() -> ScheduleGenerator:
    return object.__new__(ScheduleGenerator)


def test_missing_marker_is_rejected_in_strict_mode():
    with pytest.raises(ValueError, match="缺少状态标记"):
        _generator()._parse_busy_periods_from_schedule(SCHEDULE)


def test_lenient_mode_keeps_legacy_keyword_fallback():
    periods = _generator()._parse_busy_periods_from_schedule(SCHEDULE, strict=False)

    by_start = {p.start_time: p for p in periods}
    # 没有标记、也没有“刷手机/休息…”这类关键词 → 按忙碌处理（旧行为）
    assert by_start["15:08"].is_busy is True
    # 有明确标记的行照常解析
    assert by_start["15:52"].is_busy is False
    assert by_start["13:24"].is_busy is True
    assert by_start["01:36"].is_open_sleep


def test_rendered_text_always_ends_with_status_marker():
    periods = [
        BusyPeriod("15:08", "15:52", "小怡摆房间【主动分享】", is_busy=False),
        BusyPeriod("16:29", "17:33", "小怡拉伸", is_busy=True),
        BusyPeriod("01:36", None, "小怡睡觉", is_busy=True),
    ]

    text = ScheduleGenerator._render_schedule_text(periods)

    assert text.splitlines() == [
        "15:08-15:52 小怡摆房间【主动分享】【可回消息】",
        "16:29-17:33 小怡拉伸【忙碌】",
        "01:36 小怡睡觉【忙碌】",
    ]
    # 重建后必须能被严格解析回去（幂等）
    reparsed = _generator()._parse_busy_periods_from_schedule(text)
    assert [(p.start_time, p.is_busy) for p in reparsed] == [
        ("15:08", False),
        ("16:29", True),
        ("01:36", True),
    ]


def test_image_busy_pills_follow_stored_periods(tmp_path):
    renderer = BusyScheduleImageRenderer(tmp_path)
    entries = renderer._parse_schedule(
        SCHEDULE, date(2026, 11, 8), datetime(2026, 11, 8, 15, 31)
    )
    # 文本缺标记时，图片侧默认“可回复”——这正是本次事故的显示来源
    text_only = {e["start"]: e["busy"] for e in entries}
    assert text_only["15:08"] is False

    periods = _generator()._parse_busy_periods_from_schedule(SCHEDULE, strict=False)
    renderer._sync_busy_flags(entries, periods)

    assert [(e["start"], e["busy"]) for e in entries] == [
        ("13:24", True),
        ("15:08", True),
        ("15:52", False),
        ("16:29", True),
        ("01:36", True),
    ]


def test_image_flags_unchanged_when_text_and_periods_agree(tmp_path):
    renderer = BusyScheduleImageRenderer(tmp_path)
    text = (
        "15:08-15:52 小怡摆房间【主动分享】【可回消息】\n"
        "16:29-17:33 小怡拉伸【忙碌】\n"
        "01:36 小怡睡觉【忙碌】"
    )
    entries = renderer._parse_schedule(
        text, date(2026, 11, 8), datetime(2026, 11, 8, 15, 31)
    )
    periods = [
        BusyPeriod("15:08", "15:52", "小怡摆房间【主动分享】", is_busy=False),
        BusyPeriod("16:29", "17:33", "小怡拉伸", is_busy=True),
        BusyPeriod("01:36", None, "小怡睡觉", is_busy=True),
    ]

    renderer._sync_busy_flags(entries, periods)

    assert [(e["start"], e["busy"]) for e in entries] == [
        ("15:08", False),
        ("16:29", True),
        ("01:36", True),
    ]


def test_image_flags_fall_back_to_text_without_periods():
    renderer = BusyScheduleImageRenderer(".")
    entries = renderer._parse_schedule(
        SCHEDULE, date(2026, 11, 8), datetime(2026, 11, 8, 15, 31)
    )

    renderer._sync_busy_flags(entries, None)  # 旧数据/裸对象不应报错

    assert {e["start"]: e["busy"] for e in entries}["16:29"] is True
