"""Atomic editing for the active schedule cycle."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .data import BusyPeriod, ScheduleData, parse_clock_time

IMPORTANT_ACTIVITY_KEYWORDS = (
    "早餐",
    "午餐",
    "晚餐",
    "吃饭",
    "外卖",
    "做饭",
    "洗澡",
    "睡觉",
    "睡眠",
    "就寝",
    "约定",
    "答应",
)


class ScheduleEditError(ValueError):
    """Base error for a rejected schedule edit."""

    code = "invalid_edit"


class ScheduleEditConflict(ScheduleEditError):
    """The requested edit conflicts with the protected timeline."""

    code = "conflict"


class ScheduleEditNeedsConfirmation(ScheduleEditError):
    """The edit removes an important activity without confirmation."""

    code = "needs_confirmation"


@dataclass(frozen=True)
class ScheduleEditResult:
    """A validated schedule replacement that has not been persisted yet."""

    data: ScheduleData
    changes: tuple[str, ...]


def _absolute_time(
    value: str, owner_date: date, schedule_time: tuple[int, int]
) -> datetime:
    hour, minute = parse_clock_time(value)
    target_date = (
        owner_date + timedelta(days=1) if (hour, minute) < schedule_time else owner_date
    )
    return datetime.combine(target_date, datetime.min.time()).replace(
        hour=hour, minute=minute
    )


def _period_bounds(
    period: BusyPeriod, owner_date: date, schedule_time: tuple[int, int]
) -> tuple[datetime, datetime | None]:
    start = _absolute_time(period.start_time, owner_date, schedule_time)
    if period.end_time is None:
        return start, None
    end = _absolute_time(period.end_time, owner_date, schedule_time)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _period_state(
    period: BusyPeriod,
    owner_date: date,
    schedule_time: tuple[int, int],
    now: datetime,
) -> str:
    start, end = _period_bounds(period, owner_date, schedule_time)
    if start > now:
        return "future"
    if end is None or now < end:
        return "current"
    return "past"


def _find_period(periods: list[BusyPeriod], operation: dict[str, Any]) -> int:
    target_start = str(operation.get("target_start_time", "")).strip()
    target_activity = str(operation.get("target_activity", "")).strip()
    if not target_start:
        raise ScheduleEditError("update/remove requires target_start_time")

    matches = [
        index
        for index, period in enumerate(periods)
        if period.start_time == target_start
        and (not target_activity or target_activity in period.activity)
    ]
    if len(matches) != 1:
        raise ScheduleEditConflict(
            f"activity target is not unique or no longer exists: {target_start}"
        )
    return matches[0]


def _new_activity(operation: dict[str, Any]) -> BusyPeriod:
    start_time = str(operation.get("start_time", "")).strip()
    end_time = str(operation.get("end_time", "")).strip()
    activity = str(operation.get("activity", "")).strip()
    if not start_time or not end_time or not activity:
        raise ScheduleEditError("add requires start_time, end_time and activity")
    parse_clock_time(start_time)
    parse_clock_time(end_time)
    return BusyPeriod(
        start_time=start_time,
        end_time=end_time,
        activity=activity,
        is_busy=bool(operation.get("is_busy", False)),
        period_type="activity",
    )


def _render_schedule(periods: list[BusyPeriod]) -> str:
    lines = []
    for period in periods:
        marker = "忙碌" if period.is_busy else "可回消息"
        if period.is_open_sleep:
            lines.append(f"{period.start_time} {period.activity}【{marker}】")
        else:
            lines.append(
                f"{period.start_time}-{period.end_time} {period.activity}【{marker}】"
            )
    return "\n".join(lines)


def _validate_timeline(
    periods: list[BusyPeriod], owner_date: date, schedule_time: tuple[int, int]
) -> None:
    if len(periods) < 2 or not periods[-1].is_open_sleep:
        raise ScheduleEditConflict(
            "schedule must retain ordinary activities and one final open sleep"
        )

    previous_end = None
    for index, period in enumerate(periods):
        start, end = _period_bounds(period, owner_date, schedule_time)
        if period.is_sleep and index != len(periods) - 1:
            raise ScheduleEditConflict("open sleep must remain the final activity")
        if previous_end is not None and start < previous_end:
            raise ScheduleEditConflict(
                f"activity overlaps the previous activity: {period.start_time}"
            )
        if end is not None and end <= start:
            raise ScheduleEditConflict(
                f"activity end must be later than start: {period.start_time}"
            )
        previous_end = end


def _sorted_periods(
    periods: list[BusyPeriod], owner_date: date, schedule_time: tuple[int, int]
) -> list[BusyPeriod]:
    activities = [period for period in periods if not period.is_sleep]
    sleep = [period for period in periods if period.is_sleep]
    activities.sort(
        key=lambda period: _absolute_time(period.start_time, owner_date, schedule_time)
    )
    return activities + sleep


class ScheduleEditor:
    """Apply validated operations to a copy of one completed schedule."""

    def apply(
        self,
        data: ScheduleData,
        operations: list[dict[str, Any]],
        *,
        owner_date: date,
        schedule_time: tuple[int, int],
        now: datetime,
        confirmed_important: bool = False,
    ) -> ScheduleEditResult:
        if data.status != "completed":
            raise ScheduleEditConflict("current cycle has no completed schedule")
        if not operations:
            raise ScheduleEditError("operations must not be empty")

        updated = deepcopy(data)
        periods = deepcopy(data.busy_periods)
        changes: list[str] = []

        for operation in operations:
            action = str(operation.get("action", "")).strip().lower()
            if action == "add":
                period = _new_activity(operation)
                if _period_state(period, owner_date, schedule_time, now) != "future":
                    raise ScheduleEditConflict(
                        "new activities must start in the future"
                    )
                periods.append(period)
                changes.append(f"added {period.start_time} {period.activity}")
                continue

            if action == "set_outfit":
                outfit = str(operation.get("outfit", "")).strip()
                if not outfit:
                    raise ScheduleEditError("set_outfit requires outfit")
                updated.outfit = outfit
                updated.outfit_style = str(
                    operation.get("outfit_style", updated.outfit_style)
                ).strip()
                if "hairstyle" in operation:
                    updated.hairstyle = str(operation.get("hairstyle", "")).strip()
                changes.append("updated current outfit")
                continue

            if action not in {"update", "remove"}:
                raise ScheduleEditError(f"unsupported action: {action}")

            index = _find_period(periods, operation)
            period = periods[index]
            state = _period_state(period, owner_date, schedule_time, now)
            if state == "past":
                raise ScheduleEditConflict("past activities are locked")

            if action == "remove":
                if state == "current":
                    raise ScheduleEditConflict("the current activity cannot be removed")
                if period.is_sleep:
                    raise ScheduleEditConflict(
                        "open sleep cannot be removed; only its start_time may be adjusted"
                    )
                important = bool(operation.get("important", False)) or any(
                    keyword in period.activity
                    for keyword in IMPORTANT_ACTIVITY_KEYWORDS
                )
                if important and not confirmed_important:
                    raise ScheduleEditNeedsConfirmation(
                        f"removing important activity requires confirmation: {period.activity}"
                    )
                periods.pop(index)
                changes.append(f"removed {period.start_time} {period.activity}")
                continue

            if state == "current":
                if period.is_sleep:
                    raise ScheduleEditConflict(
                        "active open sleep cannot be edited because its end comes from the next cycle"
                    )
                forbidden = {
                    "start_time",
                    "activity",
                    "is_busy",
                }.intersection(operation)
                if forbidden:
                    raise ScheduleEditConflict(
                        "the current activity only allows changing end_time"
                    )
                end_time = str(operation.get("end_time", "")).strip()
                if not end_time:
                    raise ScheduleEditError(
                        "updating the current activity requires end_time"
                    )
                parse_clock_time(end_time)
                candidate = deepcopy(period)
                candidate.end_time = end_time
                _, candidate_end = _period_bounds(candidate, owner_date, schedule_time)
                if candidate_end is None or candidate_end <= now:
                    raise ScheduleEditConflict(
                        "the current activity end must remain in the future"
                    )
                period.end_time = end_time
            else:
                if period.is_sleep:
                    forbidden = {
                        "end_time",
                        "activity",
                        "is_busy",
                    }.intersection(operation)
                    if forbidden or "start_time" not in operation:
                        raise ScheduleEditConflict(
                            "open sleep only allows changing start_time"
                        )
                if "start_time" in operation:
                    period.start_time = str(operation["start_time"]).strip()
                    parse_clock_time(period.start_time)
                if "end_time" in operation:
                    period.end_time = str(operation["end_time"]).strip()
                    parse_clock_time(period.end_time)
                if "activity" in operation:
                    activity = str(operation["activity"]).strip()
                    if not activity:
                        raise ScheduleEditError("activity must not be empty")
                    period.activity = activity
                if "is_busy" in operation:
                    period.is_busy = bool(operation["is_busy"])
                if _period_state(period, owner_date, schedule_time, now) != "future":
                    raise ScheduleEditConflict(
                        "updated future activity must remain in the future"
                    )
            changes.append(f"updated {period.start_time} {period.activity}")

        periods = _sorted_periods(periods, owner_date, schedule_time)
        _validate_timeline(periods, owner_date, schedule_time)
        updated.busy_periods = periods
        updated.schedule = _render_schedule(periods)
        return ScheduleEditResult(updated, tuple(changes))
