from datetime import date

from core.prompt_injector import PromptInjector


def test_calendar_injection_contains_all_context_fields():
    injector = PromptInjector(
        {
            "基础设置": {"enable_calendar_context_injection": True},
            "special_day_rules": [
                {"title": "纪念日", "date": "2026-08-19"},
            ],
        }
    )

    block = injector.build_calendar_injection(date(2026, 8, 19))
    assert "<character_calendar>" in block
    assert "2026年08月19日" in block
    assert "星期三" in block
    assert "农历七月初七" in block
    assert "七夕节" in block
    assert "纪念日" in block


def test_calendar_injection_can_be_disabled_without_affecting_generation_context():
    injector = PromptInjector(
        {"基础设置": {"enable_calendar_context_injection": False}}
    )

    assert injector.build_calendar_injection(date(2026, 8, 19)) == ""


def test_always_mode_omits_unmatched_holiday_and_custom_day_labels():
    injector = PromptInjector({"calendar_context_injection_mode": "始终注入"})

    block = injector.build_calendar_injection(date(2026, 8, 20))

    assert "2026年08月20日" in block
    assert "星期四" in block
    assert "农历七月初八" in block
    assert "节日：" not in block
    assert "特别日" not in block
    assert "无已知节日" not in block
    assert "无自定义特别日" not in block


def test_notable_only_mode_injects_only_matched_events():
    injector = PromptInjector(
        {
            "calendar_context_injection_mode": "仅在有节日或特别日时注入",
            "special_day_rules": [
                {"title": "纪念日", "date": "2026-08-20"},
            ],
        }
    )

    ordinary = injector.build_calendar_injection(date(2026, 8, 21))
    notable = injector.build_calendar_injection(date(2026, 8, 20))

    assert ordinary == ""
    assert "## 今日特别日" in notable
    assert "纪念日" in notable
    assert "2026年08月20日" not in notable
    assert "星期" not in notable
    assert "农历" not in notable
