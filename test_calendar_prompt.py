import asyncio
import sys
from datetime import date
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent
PROJECT_DIR = PLUGIN_DIR.parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
if str(PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR.parent))
if getattr(sys.modules.get("astrbot"), "__path__", None) is None:
    sys.modules.pop("astrbot", None)
    sys.modules.pop("astrbot.api", None)

from astrbot_plugin_busy_schedule.core.generator import ScheduleGenerator  # noqa: E402


def test_schedule_prompt_replaces_all_calendar_placeholders():
    generator = ScheduleGenerator.__new__(ScheduleGenerator)
    generator.config = {
        "日程生成": {
            "prompt_template": (
                "{date_str}|{weekday}|{holiday}|{lunar_date}|{special_day}|"
                "{special_days}|{today_summary}|{persona_desc}|{emotion_context}|"
                "{daily_theme}|{mood_color}|{outfit_style}|{schedule_type}|"
                "{history_schedules}|{recent_chats}|{last_yesterday_activity}|"
                "{rag_context}|{weather_forecast}"
            ),
            "special_day_rules": [
                {"title": "纪念日", "date": "2026-08-19"},
            ],
        }
    }

    async def text_stub(*_args, **_kwargs):
        return "stub"

    generator._get_persona_desc = text_stub
    generator._get_emotion_context = text_stub
    generator._get_recent_chats = text_stub
    generator._get_rag_context = text_stub
    generator._get_history_schedules = lambda _target: "history"
    generator._get_yesterday_last_activity = lambda _target: "yesterday"

    prompt = asyncio.run(
        generator._build_prompt(
            date(2026, 8, 19),
            creative_context={
                "daily_theme": "theme",
                "mood_color": "mood",
                "outfit_style": "outfit",
                "schedule_type": "schedule",
            },
        )
    )

    assert "2026年08月19日|星期三|七夕节|农历七月初七|七夕节" in prompt
    assert "纪念日" in prompt
    assert "{" not in prompt and "}" not in prompt
