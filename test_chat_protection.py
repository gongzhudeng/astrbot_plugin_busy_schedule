import importlib
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import ModuleType, SimpleNamespace


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

busy_manager_module = importlib.import_module("core.busy_manager")
chat_protection_module = importlib.import_module("core.chat_protection")
message_input_module = importlib.import_module("core.message_input")

BusyPeriodManager = busy_manager_module.BusyPeriodManager
is_natural_spark_proactive = chat_protection_module.is_natural_spark_proactive
is_usable_assistant_response = chat_protection_module.is_usable_assistant_response
is_slash_prefixed_message = message_input_module.is_slash_prefixed_message


class DataManagerStub:
    pass


class EventStub:
    def __init__(self, **extras):
        self.extras = extras

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)


@dataclass
class TextComponent:
    text: str


class NonTextComponent:
    pass


def make_manager(protect_minutes=10):
    return BusyPeriodManager(
        {"忙碌时段": {"chat_protect_minutes": protect_minutes}},
        DataManagerStub(),
    )


def test_chat_model_activity_starts_protection_window():
    manager = make_manager()
    manager._last_chat_model_activity_time = datetime.now() - timedelta(minutes=9)

    assert not manager._can_enter_busy(datetime.now())

    manager._last_chat_model_activity_time = datetime.now() - timedelta(minutes=11)
    assert manager._can_enter_busy(datetime.now())


def test_inflight_replies_block_busy_entry_independently():
    manager = make_manager()
    manager.mark_reply_inflight(1)
    manager.mark_reply_inflight(2)

    manager.clear_reply_inflight(1)
    assert not manager._can_enter_busy(datetime.now())

    manager.clear_reply_inflight(2)
    assert manager._can_enter_busy(datetime.now())


def test_expired_inflight_reply_does_not_block_busy_entry():
    manager = make_manager()
    manager._reply_inflight[1] = datetime.now() - timedelta(seconds=1)

    assert manager._can_enter_busy(datetime.now())
    assert manager._reply_inflight == {}


def test_only_non_empty_assistant_responses_refresh_protection():
    assert is_usable_assistant_response(
        SimpleNamespace(role="assistant", completion_text="有效回复")
    )
    assert not is_usable_assistant_response(
        SimpleNamespace(role="assistant", completion_text="  ")
    )
    assert not is_usable_assistant_response(
        SimpleNamespace(role="user", completion_text="不是模型回复")
    )
    assert not is_usable_assistant_response(SimpleNamespace())


def test_only_natural_spark_proactive_requests_refresh_protection():
    assert is_natural_spark_proactive(EventStub(spark_proactive_retrieval=True))
    assert not is_natural_spark_proactive(
        EventStub(spark_proactive_retrieval=True, spark_slash_triggered=True)
    )
    assert not is_natural_spark_proactive(EventStub())


def test_slash_detection_uses_first_non_empty_text_only():
    assert is_slash_prefixed_message(
        [NonTextComponent(), TextComponent("  "), TextComponent(" /任意插件命令")]
    )
    assert not is_slash_prefixed_message(
        [TextComponent("普通消息"), TextComponent("/后续文本")]
    )
    assert not is_slash_prefixed_message([NonTextComponent(), TextComponent("  ")])
