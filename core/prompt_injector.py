"""Prompt injector module - handles system prompt injection for different states."""

from datetime import datetime
from typing import Optional

from astrbot.api import logger

from .data import ScheduleData, BusyPeriod


class PromptInjector:
    """Injects appropriate prompts based on current state."""

    def __init__(self, config: dict):
        self.config = config

    def _cfg(self, key: str, default=None):
        """Get config value with nested group fallback."""
        for group_name in ["基础设置", "忙碌时段", "关键词设置", "消息合并", "智能判断", "日程生成"]:
            group = self.config.get(group_name, {})
            if isinstance(group, dict) and key in group:
                val = group[key]
                if val is not None and val != "" and val != {} and val != []:
                    return val
        value = self.config.get(key)
        if value is not None and value != "" and value != {} and value != []:
            return value
        return default

    def build_custom_injection(self) -> str:
        """Build custom user-defined injection from config."""
        custom = self._cfg("custom_prompt", "")
        if not custom:
            return ""
        return f"<character_custom>\n{custom}\n</character_custom>"

    def build_static_injection(self, data: ScheduleData) -> str:
        """Build static (cacheable) prompt injection - outfit and schedule.
        
        This part changes only once per day and should be placed after persona prompt
        for optimal caching.
        """
        if not data or data.status != "completed":
            return ""

        parts = [
            "<character_static>",
            "## 今日穿搭",
        ]
        outfit_text = data.outfit if data.outfit else "未设置"
        if data.hairstyle:
            outfit_text += f"\n发型：{data.hairstyle}"
        parts.append(outfit_text)
        parts += [
            "",
            "## 今日日程安排",
            data.schedule if data.schedule else "未安排",
            "</character_static>",
        ]

        return "\n".join(parts)

    def build_busy_state_injection(
        self,
        busy_period: BusyPeriod,
    ) -> str:
        """Build dynamic injection for busy state (busy flag only).

        Activity info is already in build_schedule_injection, so this only
        carries the busy flag and changes per request while in busy mode.
        """
        return f"<character_busy>\n## 当前处于忙碌状态，正在{busy_period.activity}\n</character_busy>"

    def build_schedule_injection(
        self,
        data: ScheduleData,
        current_time: Optional[datetime] = None,
    ) -> str:
        """Build semi-static schedule injection (current activity + next activity + start time).

        This changes only on activity transitions (~10-12 times/day), making it
        very cache-friendly. Placed right after static injection (outfit + schedule)
        and before LLM persona.
        """
        if not data or data.status != "completed":
            return ""

        now = current_time or datetime.now()

        # Find current activity
        current_activity = self._find_current_activity(data, now)

        # Find next upcoming activity (busy or free)
        next_activity = None
        next_start = None
        if data.busy_periods:
            for period in sorted(data.busy_periods, key=lambda p: p.start_time):
                if period.start_datetime > now:
                    next_activity = period.activity
                    next_start = period.start_time
                    break

        parts = [
            "<character_schedule>",
            f"## 当前活动：{current_activity}" if current_activity else "## 当前活动：自由时间",
        ]

        if next_activity and next_start:
            parts.append(f"## 下一个活动：{next_activity}（{next_start}开始）")

        parts.append("</character_schedule>")

        return "\n".join(parts)

    def build_busy_exit_injection(
        self,
        merged_message: str,
        busy_period: BusyPeriod,
    ) -> str:
        """Build prompt injection when exiting busy state with merged messages."""
        injection_parts = [
            "",
            "=" * 40,
            f"【忙碌时段结束 - 你刚才在{busy_period.activity}】",
            "",
            merged_message,
            "",
            "=" * 40,
            "",
        ]

        return "\n".join(injection_parts)

    def _find_current_activity(
        self, data: ScheduleData, current_time: datetime
    ) -> Optional[str]:
        """Find the current activity from schedule based on time."""
        if not data.busy_periods:
            # Try to parse from schedule text
            return self._parse_activity_from_text(data.schedule, current_time)

        current_hour = current_time.hour
        current_minute = current_time.minute
        current_total_minutes = current_hour * 60 + current_minute

        for period in data.busy_periods:
            start_parts = period.start_time.split(":")
            end_parts = period.end_time.split(":")

            start_minutes = int(start_parts[0]) * 60 + int(start_parts[1])
            end_minutes = int(end_parts[0]) * 60 + int(end_parts[1])

            if start_minutes <= current_total_minutes < end_minutes:
                return period.activity

        return None

    def _parse_activity_from_text(
        self, schedule_text: str, current_time: datetime
    ) -> Optional[str]:
        """Parse activity from schedule text (fallback method)."""
        if not schedule_text:
            return None

        current_hour = current_time.hour
        current_minute = current_time.minute

        # Simple parsing: look for time patterns
        import re
        pattern = r"(\d{1,2}):(\d{2})\s*[-~]\s*(\d{1,2}):(\d{2})\s+(.+?)(?:\n|$)"

        for match in re.finditer(pattern, schedule_text):
            start_hour = int(match.group(1))
            start_min = int(match.group(2))
            end_hour = int(match.group(3))
            end_min = int(match.group(4))
            activity = match.group(5).strip()

            start_total = start_hour * 60 + start_min
            end_total = end_hour * 60 + end_min
            current_total = current_hour * 60 + current_minute

            if start_total <= current_total < end_total:
                # Remove busy markers
                activity = re.sub(r"【.*?】", "", activity).strip()
                return activity

        return None