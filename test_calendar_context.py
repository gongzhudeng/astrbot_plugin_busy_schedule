from datetime import date

from core.calendar_context import (
    build_calendar_context,
    get_holiday,
    get_lunar_date_cn,
)


def test_qixi_calendar_context_is_deterministic_without_optional_dependencies():
    target = date(2026, 8, 19)

    assert get_lunar_date_cn(target) == "农历七月初七"
    assert get_holiday(target) == "七夕节"
    context = build_calendar_context(target)
    assert context["date_str"] == "2026年08月19日"
    assert context["weekday"] == "星期三"
    assert context["holiday"] == "七夕节"
    assert context["special_day"] == "七夕节"


def test_solar_lunar_and_leap_month_dates_are_rendered_correctly():
    assert get_holiday(date(2026, 1, 1)) == "元旦"
    assert get_holiday(date(2026, 2, 17)) == "春节"
    assert get_lunar_date_cn(date(2025, 7, 25)) == "农历闰六月初一"


def test_special_day_rules_support_exact_annual_monthly_and_weekly_matches():
    target = date(2026, 8, 19)
    config = {
        "special_day_rules": [
            {"title": "低优先级周三", "weekday": "星期三", "priority": 1},
            {"title": "纪念日", "month": 8, "day": 19, "priority": 5},
            {"title": "每月提醒", "day": 19},
            {"title": "指定日期", "date": "2026-08-19", "priority": 3},
            {"title": "纪念日", "date": "2026-08-19", "priority": 2},
            {"title": "不应出现", "date": "2026-08-18"},
            {"title": "已禁用", "date": "2026-08-19", "enabled": False},
        ]
    }

    context = build_calendar_context(target, config)
    assert context["special_days"].splitlines() == [
        "- 纪念日",
        "- 指定日期",
        "- 低优先级周三",
        "- 每月提醒",
    ]


def test_invalid_special_day_rules_are_ignored_and_empty_values_are_explicit():
    target = date(2026, 8, 20)
    config = {
        "special_day_rules": [
            {"title": "坏月份", "month": 13, "day": 20},
            {"title": "坏日期", "date": "not-a-date"},
            {"title": "坏星期", "weekday": "星期八"},
        ]
    }

    context = build_calendar_context(target, config)
    assert context["holiday"] == "无已知节日"
    assert context["special_day"] == "无特别日"
    assert context["special_days"] == "无自定义特别日"
