"""Schedule data management module."""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from astrbot.api import logger


DEFAULT_SCHEDULE_TIME = (7, 0)
_WARNED_INVALID_SCHEDULE_TIMES: set[str] = set()


def parse_schedule_time(value: object) -> tuple[int, int]:
    """Parse a validated HH:MM schedule boundary."""
    try:
        hour_text, minute_text = str(value).strip().split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
        return hour, minute
    except (TypeError, ValueError):
        invalid_value = repr(value)
        if invalid_value not in _WARNED_INVALID_SCHEDULE_TIMES:
            _WARNED_INVALID_SCHEDULE_TIMES.add(invalid_value)
            logger.warning(
                f"[BusySchedule] Invalid schedule_time {invalid_value}; using 07:00"
            )
        return DEFAULT_SCHEDULE_TIME


def get_schedule_owner_date(
    now: datetime, schedule_time: tuple[int, int]
) -> date:
    """Return the schedule cycle that owns a concrete moment."""
    hour, minute = schedule_time
    boundary = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now.date() if now >= boundary else now.date() - timedelta(days=1)


@dataclass
class BusyPeriod:
    """Represents a busy or available period in a schedule."""

    start_time: str  # HH:MM format
    end_time: Optional[str]
    activity: str
    is_busy: bool = True
    period_type: str = "activity"

    def __post_init__(self):
        if self.end_time is None:
            self.period_type = "sleep"
        elif self.period_type not in {"activity", "sleep"}:
            self.period_type = "activity"

    @property
    def is_sleep(self) -> bool:
        """Return whether this period is structurally marked as sleep."""
        return self.period_type == "sleep"

    @property
    def is_open_sleep(self) -> bool:
        """Return whether this period is an open-ended sleep entry."""
        return self.is_sleep and self.end_time is None

    def to_absolute_datetimes(
        self,
        owner_date: date,
        schedule_h: int,
        schedule_m: int,
        resolved_end: Optional[datetime] = None,
    ) -> tuple[datetime, datetime]:
        """Expand this period into absolute datetimes for a schedule cycle."""
        s_h, s_m = parse_clock_time(self.start_time)
        next_day = owner_date + timedelta(days=1)
        base_start = next_day if (s_h, s_m) < (schedule_h, schedule_m) else owner_date
        start = datetime(base_start.year, base_start.month, base_start.day, s_h, s_m)

        if self.end_time is None:
            if resolved_end is None or resolved_end <= start:
                raise ValueError("Open sleep requires a later wake time")
            return start, resolved_end

        e_h, e_m = parse_clock_time(self.end_time)
        base_end = next_day if (e_h, e_m) < (schedule_h, schedule_m) else owner_date
        end = datetime(base_end.year, base_end.month, base_end.day, e_h, e_m)
        if end <= start:
            end += timedelta(days=1)
        return start, end

    def contains(
        self,
        time: datetime,
        owner_date: Optional[date] = None,
        schedule_time: tuple[int, int] = (7, 0),
        resolved_end: Optional[datetime] = None,
    ) -> bool:
        """Check whether time falls inside this resolved period."""
        base = owner_date if owner_date is not None else time.date()
        start, end = self.to_absolute_datetimes(
            base, *schedule_time, resolved_end=resolved_end
        )
        return start <= time < end


@dataclass(frozen=True)
class ActiveSchedule:
    """A completed schedule projected onto an effective cycle."""

    source_owner_date: date
    owner_date: date
    data: "ScheduleData"


@dataclass(frozen=True)
class ResolvedPeriod:
    """A schedule period with an absolute, closed time range."""

    owner_date: date
    period: BusyPeriod
    start: datetime
    end: datetime

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


def parse_clock_time(value: str) -> tuple[int, int]:
    """Parse and validate an HH:MM activity time."""
    hour_text, minute_text = value.strip().split(":", 1)
    hour, minute = int(hour_text), int(minute_text)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"Invalid activity time: {value!r}")
    return hour, minute


def get_first_activity_start(
    data: "ScheduleData", owner_date: date, schedule_time: tuple[int, int]
) -> datetime:
    """Return the first ordinary activity start on an effective timeline."""
    starts = []
    for period in data.busy_periods:
        if period.is_sleep:
            continue
        hour, minute = parse_clock_time(period.start_time)
        base = (
            owner_date + timedelta(days=1)
            if (hour, minute) < schedule_time
            else owner_date
        )
        starts.append(datetime(base.year, base.month, base.day, hour, minute))
    if not starts:
        raise ValueError("Schedule has no ordinary activity")
    return min(starts)


def resolve_schedule_periods(
    active: ActiveSchedule,
    schedule_time: tuple[int, int],
    next_active: Optional[ActiveSchedule] = None,
) -> list[ResolvedPeriod]:
    """Resolve one effective schedule, including its open sleep end."""
    wake_time = None
    if any(period.is_open_sleep for period in active.data.busy_periods):
        if next_active is None:
            raise ValueError("Open sleep requires the next effective schedule")
        wake_time = get_first_activity_start(
            next_active.data, next_active.owner_date, schedule_time
        )

    resolved = []
    for period in active.data.busy_periods:
        start, end = period.to_absolute_datetimes(
            active.owner_date,
            *schedule_time,
            resolved_end=wake_time if period.is_open_sleep else None,
        )
        resolved.append(ResolvedPeriod(active.owner_date, period, start, end))
    return sorted(resolved, key=lambda item: item.start)


@dataclass
class ScheduleData:
    """Represents a day's schedule data."""

    date: str  # YYYY-MM-DD format
    outfit_style: str = ""
    outfit: str = ""
    hairstyle: str = ""  # optional, e.g. "双马尾"; empty means use reference image default
    schedule: str = ""
    busy_periods: list[BusyPeriod] = field(default_factory=list)
    status: str = "pending"  # pending, generating, completed, failed
    last_updated: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduleData":
        """Create from dictionary while accepting legacy closed sleep entries."""
        payload = dict(data)
        busy_periods_data = payload.pop("busy_periods", [])
        busy_periods = []
        legacy_sleep_keywords = ("睡觉", "睡眠", "就寝", "入睡", "休眠")
        for item in busy_periods_data:
            period = dict(item)
            period.setdefault("end_time", None)
            if "period_type" not in period:
                activity = str(period.get("activity", ""))
                period["period_type"] = (
                    "sleep"
                    if period["end_time"] is None
                    or any(keyword in activity for keyword in legacy_sleep_keywords)
                    else "activity"
                )
            busy_periods.append(BusyPeriod(**period))
        return cls(**payload, busy_periods=busy_periods)


class ScheduleDataManager:
    """Manages schedule data persistence."""

    def __init__(self, data_file: Path):
        self.data_file = data_file
        self._data: dict[str, ScheduleData] = {}
        self._load()

    def _load(self):
        """Load data from file."""
        if not self.data_file.exists():
            return
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            for date_str, item in raw_data.items():
                self._data[date_str] = ScheduleData.from_dict(item)
            logger.info(f"[BusySchedule] Loaded {len(self._data)} schedule data entries")
        except Exception as e:
            logger.error(f"[BusySchedule] Failed to load schedule data: {e}")

    def _save(self):
        """Save data to file."""
        try:
            self.data_file.parent.mkdir(parents=True, exist_ok=True)
            data_dict = {k: v.to_dict() for k, v in self._data.items()}
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(data_dict, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[BusySchedule] Failed to save schedule data: {e}")

    def get(self, target_date: date) -> Optional[ScheduleData]:
        """Get schedule data for a specific date."""
        date_str = target_date.strftime("%Y-%m-%d")
        return self._data.get(date_str)

    def get_active(self, owner_date: date) -> Optional[ActiveSchedule]:
        """Project the newest usable completed schedule onto owner_date."""
        current = self.get(owner_date)
        if current and current.status == "completed":
            return ActiveSchedule(owner_date, owner_date, current)
        latest = self.get_latest_completed(owner_date)
        if not latest:
            return None
        source_owner_date, data = latest
        return ActiveSchedule(source_owner_date, owner_date, data)

    def get_latest_completed(
        self, target_date: date
    ) -> Optional[tuple[date, ScheduleData]]:
        """Return the newest completed schedule on or before target_date."""
        candidates = []
        for date_str, data in self._data.items():
            if data.status != "completed":
                continue
            try:
                owner_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if owner_date <= target_date:
                candidates.append((owner_date, data))
        return max(candidates, key=lambda item: item[0]) if candidates else None

    def set(self, target_date: date, data: ScheduleData):
        """Set schedule data for a specific date."""
        date_str = target_date.strftime("%Y-%m-%d")
        data.date = date_str
        data.last_updated = datetime.now().isoformat()
        self._data[date_str] = data
        self._save()

    def update_busy_periods(self, target_date: date, periods: list[BusyPeriod]):
        """Update busy periods for a specific date."""
        data = self.get(target_date)
        if data:
            data.busy_periods = periods
            data.last_updated = datetime.now().isoformat()
            self._save()

    def get_or_create(self, target_date: date) -> ScheduleData:
        """Get or create empty schedule data for a date."""
        data = self.get(target_date)
        if not data:
            data = ScheduleData(date=target_date.strftime("%Y-%m-%d"))
            self.set(target_date, data)
        return data