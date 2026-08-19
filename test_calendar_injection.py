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
