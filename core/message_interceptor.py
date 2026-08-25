"""Message interception and delayed-message metadata handling."""

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone, tzinfo
from string import Formatter
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class _TemplateValues(dict):
    """Keep user templates renderable when they contain an unknown placeholder."""

    def __missing__(self, key):
        return "{" + key + "}"


class MessageInterceptor:
    """Intercept messages during busy periods and retain receipt timestamps."""

    def __init__(
        self,
        config: dict,
        timezone: str | tzinfo | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self._timezone_spec = timezone
        self._clock = clock or datetime.now

        # Message queues per user (unified_msg_origin)
        self._message_queues: dict[str, list[dict]] = defaultdict(list)
        self._event_refs: dict[str, list[AstrMessageEvent]] = defaultdict(list)

    def _config_value(self, key: str, default):
        group = self.config.get("消息合并", {})
        if isinstance(group, dict) and key in group:
            value = group[key]
            if value is not None and value != "":
                return value
        value = self.config.get(key)
        return default if value is None or value == "" else value

    def _tzinfo(self) -> tzinfo:
        value = self._timezone_spec
        if isinstance(value, tzinfo):
            return value
        if value:
            try:
                return ZoneInfo(str(value))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[BusySchedule] Invalid AstrBot timezone %r; using local timezone: %s",
                    value,
                    exc,
                )
        return datetime.now().astimezone().tzinfo or timezone.utc

    def _now(self) -> datetime:
        now = self._clock()
        tz = self._tzinfo()
        if now.tzinfo is None:
            return now.replace(tzinfo=tz)
        return now.astimezone(tz)

    def _parse_timestamp(self, value: str | datetime) -> datetime:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self._tzinfo())
        return parsed.astimezone(self._tzinfo())

    @property
    def max_message_count(self) -> int:
        """Get max messages before force send."""
        try:
            return max(1, int(self._config_value("max_message_count", 20)))
        except (TypeError, ValueError):
            return 20

    @property
    def merge_prefix(self) -> str:
        """Get the configured delayed-message prefix template."""
        return self._config_value(
            "merge_prefix",
            "[这些是你在忙碌时段（{start_time}-{end_time}）收到的用户消息：]",
        )

    @property
    def merge_suffix(self) -> str:
        """Get the configured delayed-message suffix template."""
        return self._config_value(
            "merge_suffix",
            "[请回复用户，并说明你刚才在做什么。]",
        )

    def queue_message(
        self,
        user_id: str,
        message_text: str,
        event: AstrMessageEvent,
        extra_components: list | None = None,
    ):
        """Queue a message with a full, timezone-aware ISO receipt timestamp."""
        now = self._now()
        message_data = {
            "text": message_text,
            "timestamp": now.isoformat(timespec="seconds"),
            "extra_components": extra_components or [],
        }

        self._message_queues[user_id].append(message_data)
        self._event_refs[user_id].append(event)

        if len(self._message_queues[user_id]) >= self.max_message_count:
            logger.info(f"[BusySchedule] Max message count reached for {user_id}")
            return "force_send"
        return "queued"

    def get_queued_messages(self, user_id: str) -> list[dict]:
        """Get queued messages for a user."""
        return self._message_queues.get(user_id, [])

    def get_merged_user_message(self, user_id: str) -> str | None:
        """Return only the real delayed user text, without timestamp labels."""
        messages = self._message_queues.get(user_id, [])
        if not messages:
            return None
        texts = [str(msg.get("text", "")) for msg in messages]
        if len(texts) == 1:
            return texts[0]
        return "\n".join(
            f"消息 {index}: {text}" for index, text in enumerate(texts, start=1)
        )

    def build_delivery_payload(
        self,
        user_id: str,
        busy_start_time: str,
        busy_end_time: str,
        reason: str,
        activity: str = "",
    ) -> dict | None:
        """Snapshot queue metadata for a provider-only wake explanation."""
        messages = self._message_queues.get(user_id, [])
        if not messages:
            return None
        snapshot = [
            {
                "timestamp": str(msg.get("timestamp", "")),
            }
            for msg in messages
        ]
        received = [self._parse_timestamp(msg["timestamp"]) for msg in snapshot]
        received_start = received[0].isoformat(timespec="seconds")
        received_end = received[-1].isoformat(timespec="seconds")
        received_times = [item["timestamp"] for item in snapshot]
        return {
            "busy_start_time": busy_start_time,
            "busy_end_time": busy_end_time,
            "start_time": busy_start_time,
            "end_time": busy_end_time,
            "busy_period": {
                "start_time": busy_start_time,
                "end_time": busy_end_time,
            },
            "activity": activity or "",
            "reason": reason,
            "delivery_reason": reason,
            "messages": snapshot,
            "received_times": received_times,
            "message_count": len(snapshot),
            "received_start": received_start,
            "received_end": received_end,
            "merge_prefix": self.merge_prefix,
            "merge_suffix": self.merge_suffix,
        }

    @staticmethod
    def _format_template(template: str, values: dict) -> str:
        # format_map preserves unknown legacy/custom placeholders instead of
        # dropping a whole delayed response because one optional field is absent.
        try:
            return Formatter().vformat(str(template), (), _TemplateValues(values))
        except Exception:
            return str(template)

    def render_wake_context(
        self,
        payload: dict,
        processing_time: datetime | None = None,
    ) -> str:
        """Render temporary timing context; message bodies are intentionally omitted."""
        processing = processing_time or self._now()
        if processing.tzinfo is None:
            processing = processing.replace(tzinfo=self._tzinfo())
        else:
            processing = processing.astimezone(self._tzinfo())
        values = {
            "start_time": payload.get("busy_start_time", ""),
            "end_time": payload.get("busy_end_time", ""),
            "received_start": payload.get("received_start", ""),
            "received_end": payload.get("received_end", ""),
            "received_start_time": payload.get("received_start", ""),
            "received_end_time": payload.get("received_end", ""),
            "receive_start": payload.get("received_start", ""),
            "receive_end": payload.get("received_end", ""),
            "actual_received_start": payload.get("received_start", ""),
            "actual_received_end": payload.get("received_end", ""),
            "actual_received_start_time": payload.get("received_start", ""),
            "actual_received_end_time": payload.get("received_end", ""),
            "processing_time": processing.isoformat(timespec="seconds"),
            "actual_processing_time": processing.isoformat(timespec="seconds"),
            "reply_time": processing.isoformat(timespec="seconds"),
            "current_time": processing.isoformat(timespec="seconds"),
            "message_count": payload.get("message_count", 0),
        }
        prefix = str(payload.get("merge_prefix", self.merge_prefix)).replace(
            "以下是", "这些是"
        )
        suffix = str(payload.get("merge_suffix", self.merge_suffix))
        activity = payload.get("activity") or "忙碌时段"
        lines = [
            "<busy_schedule_wake_context>",
            self._format_template(prefix, values),
            "消息正文已作为本轮 user 内容提供；此处不重复正文。",
            f"忙碌时段：{values['start_time']}-{values['end_time']}；触发原因：{payload.get('reason', 'unknown')}。",
            f"活动信息：{activity}（仅用于理解延迟原因，不要求反复提及）。",
            f"实际接收时间区间：{values['received_start']} 至 {values['received_end']}。",
            *[
                f"第 {index} 条消息原始接收时间：{item.get('timestamp', '')}。"
                for index, item in enumerate(payload.get("messages", []), start=1)
            ],
            f"本轮实际处理/回复时间：{values['processing_time']}。",
            f"延迟消息数量：{values['message_count']}。",
            "AstrBot 的 Current datetime 与本轮处理时间代表真正回复时刻；每条消息的接收时间代表用户原消息到达时刻。",
            "理解“刚才、现在、今天、明天”等相对时间时，先按对应原始接收时间理解，再结合本轮当前时间和状态回复；跨午夜按完整日期判断。",
            self._format_template(suffix, values),
            "</busy_schedule_wake_context>",
        ]
        return "\n".join(lines)

    def get_merged_message(
        self, user_id: str, busy_start_time: str, busy_end_time: str
    ) -> str | None:
        """Compatibility rendering for callers that still expect a wrapped message."""
        user_message = self.get_merged_user_message(user_id)
        if user_message is None:
            return None
        payload = self.build_delivery_payload(
            user_id, busy_start_time, busy_end_time, reason="legacy"
        )
        if payload is None:
            return None
        values = {
            "start_time": busy_start_time,
            "end_time": busy_end_time,
            "received_start": payload["received_start"],
            "received_end": payload["received_end"],
            "received_start_time": payload["received_start"],
            "received_end_time": payload["received_end"],
            "receive_start": payload["received_start"],
            "receive_end": payload["received_end"],
            "processing_time": self._now().isoformat(timespec="seconds"),
            "reply_time": self._now().isoformat(timespec="seconds"),
            "message_count": payload["message_count"],
        }
        prefix = str(self.merge_prefix).replace("以下是", "这些是")
        return "\n".join(
            [
                self._format_template(prefix, values),
                user_message,
                self._format_template(self.merge_suffix, values),
            ]
        )

    def get_extra_components(self, user_id: str) -> list:
        """Get all extra components (images, etc.) for a user."""
        components = []
        for msg in self._message_queues.get(user_id, []):
            components.extend(msg.get("extra_components", []))
        return components

    def clear_queue(self, user_id: str):
        """Clear message queue for a user."""
        self._message_queues.pop(user_id, None)
        self._event_refs.pop(user_id, None)

    def get_all_queued_user_ids(self) -> list[str]:
        return list(self._message_queues.keys())

    def has_queued_messages(self, user_id: str) -> bool:
        return len(self._message_queues.get(user_id, [])) > 0

    def mark_sent(self, user_id: str):
        self.clear_queue(user_id)

    def get_queue_stats(self) -> dict:
        stats = {}
        for user_id, messages in self._message_queues.items():
            stats[user_id] = {
                "count": len(messages),
                "oldest": messages[0]["timestamp"] if messages else None,
                "newest": messages[-1]["timestamp"] if messages else None,
            }
        return stats
