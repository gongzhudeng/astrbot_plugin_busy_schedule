from datetime import date

from core.calendar_context import (
    build_calendar_context,
    get_holiday,
    get_lunar_date_cn,
    get_work_status,
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


def test_special_day_rule_modes_match_only_their_own_fields():
    target = date(2026, 8, 19)  # Wednesday, not lunar 8-24
    config = {
        "special_day_rules": [
            {"title": "界面每周", "rule_type": "每周重复", "weekday": ["星期三", "星期日"]},
            {"title": "界面每年", "rule_type": "每年或每月重复", "month": 8, "day": 19},
            {"title": "界面每月", "rule_type": "每年或每月重复", "month": 0, "day": 19},
            {"title": "界面只此一天", "rule_type": "只此一天", "date": "2026-08-19"},
            {
                "title": "模式外字段忽略",
                "rule_type": "只此一天",
                "date": "2026-08-19",
                "month": 3,
            },
            {"title": "其他日期", "rule_type": "只此一天", "date": "2026-08-18"},
            {"title": "已禁用", "rule_type": "每周重复", "weekday": ["星期三"], "enabled": False},
            {"title": "旧格式星期", "weekday": "星期三"},
            {"title": "旧格式每年", "month": 8, "day": 19},
        ]
    }

    context = build_calendar_context(target, config)
    assert context["special_days"].splitlines() == [
        "- 界面每周",
        "- 界面每年",
        "- 界面每月",
        "- 界面只此一天",
        "- 模式外字段忽略",
        "- 旧格式星期",
        "- 旧格式每年",
    ]


def test_incomplete_mode_rules_and_invalid_values_are_ignored():
    target = date(2026, 8, 19)
    config = {
        "special_day_rules": [
            {"title": "坏农历月", "rule_type": "农历每年", "lunar_month": 13, "lunar_day": 24},
            {"title": "坏农历日", "rule_type": "农历每年", "lunar_month": 8, "lunar_day": 31},
            {"title": "缺农历日", "rule_type": "农历每年", "lunar_month": 8},
            {"title": "缺星期", "rule_type": "每周重复", "weekday": []},
            {"title": "坏星期", "rule_type": "每周重复", "weekday": ["星期八"]},
            {"title": "缺日期", "rule_type": "只此一天", "date": ""},
            {"title": "坏日期", "rule_type": "只此一天", "date": "not-a-date"},
            {"title": "缺月日", "rule_type": "每年或每月重复", "month": 0, "day": 0},
            {"title": "坏月份", "rule_type": "每年或每月重复", "month": 13, "day": 19},
            {"title": "无方式", "title2": None},
        ]
    }

    context = build_calendar_context(target, config)
    assert context["special_days"] == "无自定义特别日"


def test_lunar_special_day_rule_matches_lunar_anniversary():
    config = {
        "special_day_rules": [
            {"rule_type": "农历每年", "lunar_month": 8, "lunar_day": 24},
        ]
    }

    context = build_calendar_context(date(2026, 10, 4), config)
    assert get_lunar_date_cn(date(2026, 10, 4)) == "农历八月廿四"
    assert context["special_days"] == "- 农历八月廿四"
    assert build_calendar_context(date(2026, 10, 3), config)[
        "special_days"
    ] == "无自定义特别日"


def test_work_status_covers_all_work_modes():
    big_small = {
        "作息制度": {
            "work_mode": "大小周",
            "big_week_base_saturday": "2026-06-27",
            "big_week_base_type": "基准周六上班",
            "work_status_label": "Mando的作息",
            "temp_rest_dates": ["2026-07-20"],
            "temp_work_dates": ["2026-07-21"],
        }
    }
    cases = [
        (date(2026, 6, 22), "- Mando的作息：今天上班（大周，本周六上班）"),
        (date(2026, 6, 27), "- Mando的作息：今天上班（大周）"),
        (date(2026, 6, 28), "- Mando的作息：今天休息（大周，单休周）"),
        (date(2026, 6, 29), "- Mando的作息：今天上班（小周，本周六休息）"),
        (date(2026, 7, 4), "- Mando的作息：今天休息（小周，双休）"),
        (date(2026, 7, 5), "- Mando的作息：今天休息（小周，双休周）"),
        (date(2026, 7, 20), "- Mando的作息：今天休息（临时休息日）"),
        (date(2026, 7, 21), "- Mando的作息：今天上班（临时工作日）"),
    ]
    for target, expected in cases:
        assert get_work_status(target, big_small) == expected, target

    # Base type inversion: a base Saturday that was a rest day flips the parity.
    rest_base = {
        "作息制度": {
            "work_mode": "大小周",
            "big_week_base_saturday": "2026-06-27",
            "big_week_base_type": "基准周六休息",
            "work_status_label": "Mando的作息",
        }
    }
    assert get_work_status(date(2026, 6, 27), rest_base) == (
        "- Mando的作息：今天休息（小周，双休）"
    )

    # Missing or invalid anchor disables the line instead of guessing.
    assert get_work_status(date(2026, 6, 27), {"作息制度": {"work_mode": "大小周"}}) == ""
    broken = {
        "作息制度": {
            "work_mode": "大小周",
            "big_week_base_saturday": "not-a-date",
        }
    }
    assert get_work_status(date(2026, 6, 27), broken) == ""

    fixed = [
        ("双休", date(2026, 7, 3), "今天上班"),
        ("双休", date(2026, 7, 4), "今天休息（双休）"),
        ("单休", date(2026, 7, 3), "今天上班"),
        ("单休", date(2026, 7, 5), "今天休息（单休）"),
        ("无休", date(2026, 7, 5), "今天上班（无休）"),
    ]
    for work_mode, target, status in fixed:
        config = {"作息制度": {"work_mode": work_mode, "work_status_label": "Mando的作息"}}
        assert get_work_status(target, config) == f"- Mando的作息：{status}", (
            work_mode,
            target,
        )

    custom = {
        "作息制度": {
            "work_mode": "自定义",
            "custom_work_days": ["星期一", "星期三"],
        }
    }
    assert get_work_status(date(2026, 7, 1), custom).endswith("今天上班")
    assert get_work_status(date(2026, 7, 2), custom).endswith("今天休息")

    # Empty label falls back to a generic prefix.
    assert get_work_status(date(2026, 7, 3), {"作息制度": {"work_mode": "单休"}}) == (
        "- 用户的作息：今天上班"
    )

    # No configuration at all produces no line.
    assert get_work_status(date(2026, 7, 3)) == ""


def test_calendar_context_includes_work_status_line():
    config = {
        "作息制度": {
            "work_mode": "大小周",
            "big_week_base_saturday": "2026-06-27",
            "big_week_base_type": "基准周六上班",
            "work_status_label": "Mando的作息",
        }
    }

    context = build_calendar_context(date(2026, 6, 27), config)
    assert context["work_status"] == "- Mando的作息：今天上班（大周）"
    assert build_calendar_context(date(2026, 6, 27))["work_status"] == ""
