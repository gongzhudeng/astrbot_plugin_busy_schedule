"""Schedule data management module."""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from astrbot.api import logger


@dataclass
class BusyPeriod:
    """Represents a busy period in the schedule."""

    start_time: str  # HH:MM format
    end_time: str  # HH:MM format
    activity: str
    is_busy: bool = True

    @property
    def start_datetime(self) -> datetime:
        """Get start time as datetime (today)."""
        today = date.today()
        return datetime.strptime(f"{today} {self.start_time}", "%Y-%m-%d %H:%M")

    @property
    def end_datetime(self) -> datetime:
        """Get end time as datetime (today)."""
        today = date.today()
        return datetime.strptime(f"{today} {self.end_time}", "%Y-%m-%d %H:%M")

    def contains(self, time: datetime) -> bool:
        """Check if a given time is within this busy period."""
        return self.start_datetime <= time < self.end_datetime


@dataclass
class ScheduleData:
    """Represents a day's schedule data."""

    date: str  # YYYY-MM-DD format
    outfit_style: str = ""
    outfit: str = ""
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
        """Create from dictionary."""
        busy_periods_data = data.pop("busy_periods", [])
        busy_periods = [BusyPeriod(**bp) for bp in busy_periods_data]
        return cls(**data, busy_periods=busy_periods)


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