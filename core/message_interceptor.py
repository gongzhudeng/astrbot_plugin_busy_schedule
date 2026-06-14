"""Message interceptor module - handles message queueing and merging during busy periods."""

import asyncio
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
        self._message_timers: dict[str, asyncio.Task] = {}
        self._event_refs: dict[str, list[AstrMessageEvent]] = defaultdict(list)

        # State
        self._is_processing: dict[str, bool] = defaultdict(bool)

    @property
    def max_message_count(self) -> int:
        """Get max messages before force send."""
        return self.config.get("max_message_count", 20)

    @property
    def send_delay_seconds(self) -> int:
        """Get delay before sending merged message."""
        return self.config.get("send_delay_seconds", 60)

    @property
    def merge_prefix(self) -> str:
        """Get merge message prefix template."""
        return self.config.get(
            "merge_prefix",
            "[以下是你在忙碌时段（{start_time}-{end_time}）收到的用户消息：]"
        )

    @property
    def merge_suffix(self) -> str:
        """Get merge message suffix."""
        return self.config.get(
            "merge_suffix",
            "[请回复用户，并说明你刚才在做什么。]"
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

        # Reset timer
        self._reset_timer(user_id)
        return "queued"

    def _reset_timer(self, user_id: str):
        """Reset the send timer for a user."""
        # Cancel existing timer
        if user_id in self._message_timers:
            self._message_timers[user_id].cancel()

        # Start new timer
        self._message_timers[user_id] = asyncio.create_task(
            self._timer_callback(user_id)
        )

    async def _timer_callback(self, user_id: str):
        """Timer callback to send merged message after delay."""
        try:
            await asyncio.sleep(self.send_delay_seconds)
            logger.info(f"[BusySchedule] Timer expired for {user_id}")
            # The actual sending will be handled by the main plugin
            # We just mark it as ready to send
            self._is_processing[user_id] = True
        except asyncio.CancelledError:
            pass

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
        if user_id in self._message_timers:
            self._message_timers[user_id].cancel()
            del self._message_timers[user_id]
        if user_id in self._is_processing:
            del self._is_processing[user_id]

    def get_all_queued_user_ids(self) -> list[str]:
        """Get all user IDs with queued messages."""
        return list(self._message_queues.keys())

    def has_queued_messages(self, user_id: str) -> bool:
        """Check if a user has queued messages."""
        return len(self._message_queues.get(user_id, [])) > 0

    def is_ready_to_send(self, user_id: str) -> bool:
        """Check if messages are ready to be sent."""
        return self._is_processing.get(user_id, False)

    def mark_sent(self, user_id: str):
        """Mark messages as sent and clear queue."""
        self.clear_queue(user_id)

    def cancel_all_timers(self):
        """Cancel all pending timers."""
        for timer in self._message_timers.values():
            timer.cancel()
        self._message_timers.clear()

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