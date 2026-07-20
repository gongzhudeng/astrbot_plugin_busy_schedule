"""Prompt injector module - handles system prompt injection for different states."""

from datetime import datetime

from .data import BusyPeriod, ResolvedPeriod, ScheduleData


class PromptInjector:
    """Injects appropriate prompts based on current state."""

    def __init__(self, config: dict):
        self.config = config

    def _cfg(self, key: str, default=None):
        """Get config value with nested group fallback."""
        for group_name in [
            "基础设置",
            "忙碌时段",
            "关键词设置",
            "消息合并",
            "日程生成",
        ]:
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
        """Build static (cacheable) prompt injection - outfit, weather summary, and schedule.

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

        # Weather summary block between outfit and schedule
        parts.append("")
        parts.append("## 今日天气")
        weather = getattr(data, "weather", None)
        if weather is not None:
            parts.append(weather.format_summary())
        else:
            parts.append("天气暂不可用")

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

        Activity details live in their own independently managed prompt block.
        """
        return f"<character_busy>\n## 当前处于忙碌状态，正在{busy_period.activity}\n</character_busy>"

    def _get_activity_state(
        self,
        resolved_periods: list[ResolvedPeriod],
        current_time: datetime | None = None,
    ) -> tuple[ResolvedPeriod | None, ResolvedPeriod | None]:
        """Resolve current and next periods from the absolute timeline."""
        now = current_time or datetime.now()
        current_period = next(
            (item for item in resolved_periods if item.contains(now)), None
        )
        candidates = [item for item in resolved_periods if item.start > now]
        next_period = (
            min(candidates, key=lambda item: item.start) if candidates else None
        )
        return current_period, next_period

    def build_activity_injection(
        self,
        data: ScheduleData,
        resolved_periods: list[ResolvedPeriod],
        current_time: datetime | None = None,
    ) -> str:
        """Build the dynamic current/next activity block only."""
        if not data or data.status != "completed":
            return ""

        current_period, next_period = self._get_activity_state(
            resolved_periods, current_time
        )
        current_activity = current_period.period.activity if current_period else None
        parts = ["<character_activity>"]
        if next_period:
            parts.append(
                f"## 下一个活动（尚未开始）：{next_period.period.activity}"
                f"（{next_period.start.strftime('%H:%M')}开始）"
            )
        else:
            parts.append("## 下一个活动（暂无）")
        parts.append(
            f"## 当前活动（正在进行）：{current_activity}"
            if current_activity
            else "## 当前活动（自由时间）：暂无已安排活动"
        )
        parts.append("</character_activity>")
        return "\n".join(parts)

    def build_execution_injection(self, execution_record: dict | None = None) -> str:
        """Build the dynamic media execution record block only."""
        if not execution_record:
            return ""

        current_counts = execution_record.get("current_counts", {})
        cycle_counts = execution_record.get("cycle_counts", {})
        parts = ["<character_execution>"]
        if self._cfg("current_activity_execution_record_enabled", True):
            parts.append(
                "## 当前活动执行记录："
                f"拍照 {current_counts.get('image', 0)} 次，"
                f"语音 {current_counts.get('voice', 0)} 次"
            )
        if self._cfg("cycle_media_execution_stats_enabled", False):
            parts.append(
                "## 本周期累计执行记录："
                f"拍照 {cycle_counts.get('image', 0)} 次，"
                f"语音 {cycle_counts.get('voice', 0)} 次"
            )
        parts.append("</character_execution>")
        return "\n".join(parts) if len(parts) > 2 else ""

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
        self,
        resolved_periods: list[ResolvedPeriod],
        current_time: datetime,
    ) -> str | None:
        """Find the current activity on a resolved absolute timeline."""
        for resolved in resolved_periods:
            if resolved.contains(current_time):
                return resolved.period.activity
        return None

    def _parse_activity_from_text(
        self, schedule_text: str, current_time: datetime
    ) -> str | None:
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
