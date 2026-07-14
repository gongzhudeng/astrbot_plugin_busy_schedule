"""Message interceptor module - handles message queueing and merging during busy periods."""

from datetime import datetime
from typing import Optional
from collections import defaultdict

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class MessageInterceptor:
    """Intercepts and queues messages during busy periods."""

    def __init__(self, config: dict):
        self.config = config

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

    @property
    def max_message_count(self) -> int:
        """Get max messages before force send."""
        try:
            return max(1, int(self._config_value("max_message_count", 20)))
        except (TypeError, ValueError):
            return 20

    @property
    def merge_prefix(self) -> str:
        """Get merge message prefix template."""
        return self._config_value(
            "merge_prefix",
            "[以下是你在忙碌时段（{start_time}-{end_time}）收到的用户消息：]",
        )

    @property
    def merge_suffix(self) -> str:
        """Get merge message suffix."""
        return self._config_value(
            "merge_suffix",
            "[请回复用户，并说明你刚才在做什么。]",
        )

    def queue_message(
        self,
        user_id: str,
        message_text: str,
        event: AstrMessageEvent,
        extra_components: list = None,
    ):
        """Queue a message for later processing."""
        now = datetime.now()

        message_data = {
            "text": message_text,
            "timestamp": now.isoformat(),
            "extra_components": extra_components or [],
        }

        self._message_queues[user_id].append(message_data)
        self._event_refs[user_id].append(event)

        # Check if should force send
        if len(self._message_queues[user_id]) >= self.max_message_count:
            logger.info(f"[BusySchedule] Max message count reached for {user_id}")
            return "force_send"

        return "queued"

    def get_queued_messages(self, user_id: str) -> list[dict]:
        """Get queued messages for a user."""
        return self._message_queues.get(user_id, [])

    def get_merged_message(
        self, user_id: str, busy_start_time: str, busy_end_time: str
    ) -> Optional[str]:
        """Get merged message text for a user."""
        messages = self._message_queues.get(user_id, [])
        if not messages:
            return None

        # Build prefix
        prefix = self.merge_prefix.format(
            start_time=busy_start_time,
            end_time=busy_end_time,
        )

        # Merge messages with timestamps
        merged_parts = []
        for msg in messages:
            timestamp = datetime.fromisoformat(msg["timestamp"]).strftime("%H:%M")
            merged_parts.append(f"[{timestamp}] {msg['text']}")

        merged_text = "\n".join(merged_parts)

        # Combine with prefix and suffix
        return f"{prefix}\n{merged_text}\n{self.merge_suffix}"

    def get_extra_components(self, user_id: str) -> list:
        """Get all extra components (images, etc.) for a user."""
        components = []
        for msg in self._message_queues.get(user_id, []):
            components.extend(msg.get("extra_components", []))
        return components

    def clear_queue(self, user_id: str):
        """Clear message queue for a user."""
        if user_id in self._message_queues:
            del self._message_queues[user_id]
        if user_id in self._event_refs:
            del self._event_refs[user_id]

    def get_all_queued_user_ids(self) -> list[str]:
        """Get all user IDs with queued messages."""
        return list(self._message_queues.keys())

    def has_queued_messages(self, user_id: str) -> bool:
        """Check if a user has queued messages."""
        return len(self._message_queues.get(user_id, [])) > 0

    def mark_sent(self, user_id: str):
        """Mark messages as sent and clear queue."""
        self.clear_queue(user_id)

    def get_queue_stats(self) -> dict:
        """Get queue statistics."""
        stats = {}
        for user_id, messages in self._message_queues.items():
            stats[user_id] = {
                "count": len(messages),
                "oldest": messages[0]["timestamp"] if messages else None,
                "newest": messages[-1]["timestamp"] if messages else None,
            }
        return stats