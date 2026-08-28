"""Prompt injector module - handles system prompt injection for different states."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from .calendar_context import build_calendar_context

if TYPE_CHECKING:
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
            "作息制度",
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

    def build_calendar_injection(self, date_obj: date) -> str:
        """Build the legacy standalone date block for compatible callers.

        Args:
            date_obj: Local calendar date for the current request.

        Returns:
            A stable XML-like prompt block with date and special-day facts.
        """
        lines = self._build_calendar_lines(date_obj)
        if not lines:
            return ""
        return "\n".join(["<character_calendar>", *lines, "</character_calendar>"])

    def _build_calendar_lines(self, date_obj: date) -> list[str]:
        """Build only the configured calendar facts, never a changing clock time."""
        mode = self._calendar_injection_mode()
        context = build_calendar_context(date_obj, self.config)
        holiday_name = context["holiday"]
        has_holiday = holiday_name != "无已知节日"
        custom_text = context["special_days"]
        has_custom_days = custom_text != "无自定义特别日"

        if mode == "disabled":
            return []

        if mode == "notable_only":
            if not has_holiday and not has_custom_days:
                return []
            lines = ["## 今日特别日"]
            if has_holiday:
                lines.append(f"- 节日：{holiday_name}")
            if has_custom_days:
                lines.append(custom_text)
            return lines

        # Always mode: ordinary days contain only the three calendar basics.
        lines = [
            "## 今日日期",
            f"- 公历日期：{context['date_str']}",
            f"- 星期：{context['weekday']}",
            f"- 农历：{context['lunar_date'] or '未知'}",
        ]
        # Opt-in: the work status belongs to the user, so conversations only
        # carry it when explicitly enabled (generation prompt always has it).
        if self._cfg("work_status_to_chat", False) and context.get("work_status"):
            lines.append(context["work_status"])
        if has_holiday:
            lines.append(f"- 节日：{holiday_name}")
        if has_custom_days:
            lines.extend(["- 自定义特别日：", custom_text])
        return lines

    def _calendar_injection_mode(self) -> str:
        """Resolve the new mode setting while preserving the old bool option."""
        configured = self._cfg("calendar_context_injection_mode", None)
        if configured in (None, "", "跟随旧开关"):
            return (
                "always"
                if self._cfg("enable_calendar_context_injection", True) is not False
                else "disabled"
            )
        normalized = str(configured).strip().casefold()
        if normalized in {"始终注入", "always", "on"}:
            return "always"
        if normalized in {
            "仅在有节日或特别日时注入",
            "仅在有节日或特别日时",
            "notable_only",
            "notable-only",
        }:
            return "notable_only"
        if normalized in {"完全关闭", "关闭", "disabled", "off", "none"}:
            return "disabled"
        return "always"

    def build_static_injection(
        self,
        data: ScheduleData | None,
        calendar_date: date | None = None,
    ) -> str:
        """Build the stable date/outfit/weather/schedule prompt block."""
        has_schedule = bool(data and data.status == "completed")
        if calendar_date is None and has_schedule:
            try:
                calendar_date = date.fromisoformat(str(data.date))
            except (TypeError, ValueError):
                calendar_date = None

        calendar_lines = (
            self._build_calendar_lines(calendar_date) if calendar_date else []
        )
        if not calendar_lines and not has_schedule:
            return ""

        parts = ["<character_static>", *calendar_lines]
        if not has_schedule:
            parts.append("</character_static>")
            return "\n".join(parts)

        if calendar_lines:
            parts.append("")
        parts.append("## 今日穿搭")
        outfit_text = data.outfit if data.outfit else "未设置"
        if data.hairstyle:
            outfit_text += f"\n发型：{data.hairstyle}"
        parts.append(outfit_text)

        parts.extend(["", "## 今日天气"])
        weather = getattr(data, "weather", None)
        if weather is not None:
            parts.append(weather.format_summary())
        else:
            parts.append("天气暂不可用")
        parts.extend(["", "## 今日日程安排"])
        parts.append(data.schedule if data.schedule else "未安排")
        parts.append("</character_static>")
        return "\n".join(parts)

    def build_schedule_injection(self, data: ScheduleData) -> str:
        """Build the schedule block for compatibility with older callers.

        The request hook uses :meth:`build_static_injection`, so the schedule is
        not appended to the temporary user tail anymore.
        """
        if not data or data.status != "completed":
            return ""
        return "\n".join(
            [
                "<character_schedule>",
                "## 今日日程安排",
                data.schedule if data.schedule else "未安排",
                "</character_schedule>",
            ]
        )

    def build_busy_state_injection(
        self,
        busy_period: BusyPeriod,
    ) -> str:
        """Build dynamic injection for busy state (busy flag only).

        Activity details live in their own independently managed prompt block.
        """
        return "<character_busy>\n## 当前处于忙碌状态\n</character_busy>"

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
