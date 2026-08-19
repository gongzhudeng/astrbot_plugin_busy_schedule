import asyncio
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

PLUGIN_DIR = Path(__file__).parent
PROJECT_DIR = PLUGIN_DIR.parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
if str(PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR.parent))
if getattr(sys.modules.get("astrbot"), "__path__", None) is None:
    sys.modules.pop("astrbot", None)
    sys.modules.pop("astrbot.api", None)

from astrbot_plugin_busy_schedule.main import BusySchedulePlugin  # noqa: E402
from core.message_interceptor import MessageInterceptor  # noqa: E402

from astrbot.core.provider.entities import ProviderRequest  # noqa: E402


class EventStub:
    unified_msg_origin = "u"

    def __init__(self, **extras):
        self.extras = extras

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)


def test_queue_uses_configured_timezone_and_clean_multi_message_body():
    times = iter(
        [
            datetime(2026, 8, 17, 23, 58),
            datetime(2026, 8, 18, 0, 1),
        ]
    )
    interceptor = MessageInterceptor(
        {
            "消息合并": {
                "merge_prefix": "[以下是 {received_start}-{received_end} ({message_count})]"
            }
        },
        timezone="Asia/Shanghai",
        clock=lambda: next(times),
    )

    interceptor.queue_message("u", "第一条", EventStub())
    interceptor.queue_message("u", "第二条", EventStub())

    queued = interceptor.get_queued_messages("u")
    assert queued[0]["timestamp"] == "2026-08-17T23:58:00+08:00"
    assert queued[1]["timestamp"] == "2026-08-18T00:01:00+08:00"
    assert interceptor.get_merged_user_message("u") == "消息 1: 第一条\n消息 2: 第二条"

    payload = interceptor.build_delivery_payload(
        "u", "23:00", "00:05", reason="exit", activity="看电影"
    )
    assert payload["received_start"].startswith("2026-08-17T23:58:00")
    assert payload["received_end"].startswith("2026-08-18T00:01:00")
    assert payload["received_times"] == [
        "2026-08-17T23:58:00+08:00",
        "2026-08-18T00:01:00+08:00",
    ]
    assert all("text" not in item for item in payload["messages"])

    wake = interceptor.render_wake_context(
        payload,
        datetime(2026, 8, 18, 0, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert "这些是" in wake
    assert "以下是" not in wake
    assert "第一条" not in wake and "第二条" not in wake
    assert "2026-08-17T23:58:00+08:00" in wake
    assert "2026-08-18T00:01:00+08:00" in wake
    assert "2026-08-18T00:05:00+08:00" in wake
    assert "跨午夜" in wake


def test_single_message_is_not_numbered():
    interceptor = MessageInterceptor(
        {}, timezone="Asia/Shanghai", clock=lambda: datetime(2026, 8, 17, 12, 0)
    )
    interceptor.queue_message("u", "原始正文", EventStub())
    assert interceptor.get_merged_user_message("u") == "原始正文"


def test_wake_context_is_temporary_even_without_an_active_schedule():
    interceptor = MessageInterceptor(
        {}, timezone="Asia/Shanghai", clock=lambda: datetime(2026, 8, 17, 23, 58)
    )
    interceptor.queue_message("u", "跨午夜正文", EventStub())
    payload = interceptor.build_delivery_payload(
        "u", "23:00", "00:05", reason="poll", activity="看电影"
    )

    plugin = BusySchedulePlugin.__new__(BusySchedulePlugin)
    calendar_dates = []
    plugin.context = SimpleNamespace(get_config=lambda: {"timezone": "Asia/Shanghai"})
    plugin.config = {}
    plugin.interceptor = interceptor
    plugin.injector = SimpleNamespace(
        build_custom_injection=lambda: "",
        build_calendar_injection=lambda calendar_date: (
            calendar_dates.append(calendar_date)
            or "<character_calendar>日期</character_calendar>"
        ),
    )
    plugin.busy_mgr = SimpleNamespace(is_busy=False)
    plugin._schedule_target_umo = "u"
    plugin._configure_schedule_edit_tool = lambda _req: False
    plugin._get_active_schedule = lambda _now: None
    plugin._now_in_astrbot_timezone = lambda: datetime(
        2026, 8, 18, 0, 5, tzinfo=ZoneInfo("Asia/Shanghai")
    )

    request = ProviderRequest(system_prompt="persona")
    event = EventStub(busy_schedule_wake_event=payload)
    asyncio.run(plugin.on_llm_request(event, request))

    assert "BUSY_SCHEDULE_CALENDAR" in request.system_prompt
    assert calendar_dates == [datetime(2026, 8, 18).date()]
    assert "BUSY_SCHEDULE_CACHE" not in request.system_prompt
    assert "EMOTION_STATE_ANCHOR" in request.system_prompt
    assert len(request.extra_user_content_parts) == 1
    wake_part = request.extra_user_content_parts[0]
    assert wake_part._no_save is True
    assert "2026-08-17T23:58:00+08:00" in wake_part.text
    assert "2026-08-18T00:05:00+08:00" in wake_part.text
    assert "跨午夜正文" not in wake_part.text
