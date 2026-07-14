"""Busy period manager - handles busy state, sleep, and chat protection."""

from datetime import datetime, date, timedelta
from typing import Optional
from astrbot.api import logger

from .data import (
    ActiveSchedule,
    BusyPeriod,
    ResolvedPeriod,
    ScheduleDataManager,
    get_schedule_owner_date,
    parse_schedule_time,
    resolve_schedule_periods,
)


class BusyPeriodManager:
    """Manages busy periods and AI availability state."""

    def __init__(self, config: dict, data_mgr: ScheduleDataManager):
        self.config = config
        self.data_mgr = data_mgr

        # State tracking
        self._is_busy: bool = False
        self._current_busy_period: Optional[BusyPeriod] = None
        self._current_resolved_period: Optional[ResolvedPeriod] = None
        self._is_manual_period: bool = False
        self._current_busy_owner_date: Optional[date] = None
        self._current_busy_schedule_time: Optional[tuple[int, int]] = None
        self._busy_start_time: Optional[datetime] = None
        self._wakeup_time: Optional[datetime] = None  # When AI was woken up by keyword
        self._last_user_message_time: Optional[datetime] = None
        self._last_adjust_time: Optional[datetime] = None

        # Callbacks
        self._on_enter_busy = None
        self._on_exit_busy = None

    def set_callbacks(
        self,
        on_enter_busy=None,
        on_exit_busy=None,
    ):
        """Set callback functions for state changes."""
        self._on_enter_busy = on_enter_busy
        self._on_exit_busy = on_exit_busy

    @property
    def is_busy(self) -> bool:
        """Check if AI is currently busy."""
        return self._is_busy

    @property
    def current_activity(self) -> Optional[str]:
        """Get current busy activity description."""
        if self._current_busy_period:
            return self._current_busy_period.activity
        return None

    def update_last_message_time(self):
        """Update the time of last user message."""
        self._last_user_message_time = datetime.now()



    def _can_enter_busy(self, now: datetime) -> bool:
        """Check if AI can enter busy state (chat protection)."""
        protect_minutes = self._config_value("chat_protect_minutes", 10)

        if self._last_user_message_time:
            inactive_minutes = (now - self._last_user_message_time).total_seconds() / 60
            return inactive_minutes >= protect_minutes

        return True

    def _is_in_wakeup_cooldown(self, now: datetime) -> bool:
        """Check if AI is in wakeup cooldown period."""
        if not self._wakeup_time:
            return False

        cooldown_minutes = self._config_value("wake_cooldown_minutes", 15)
        elapsed = (now - self._wakeup_time).total_seconds() / 60
        return elapsed < cooldown_minutes

    def _config_value(self, key: str, default=None):
        """Read flat or grouped config values consistently with the plugin."""
        for group_name in ["基础设置", "忙碌时段"]:
            group = self.config.get(group_name, {})
            if isinstance(group, dict) and key in group:
                return group[key]
        return self.config.get(key, default)

    def _parse_schedule_time(self) -> tuple[int, int]:
        """Return the validated schedule boundary."""
        return parse_schedule_time(self._config_value("schedule_time", "07:00"))

    def _get_schedule_owner_date(self, now: datetime) -> date:
        """Return the schedule-cycle date that owns the given moment."""
        return get_schedule_owner_date(now, self._parse_schedule_time())

    def _get_active_schedule(self, now: datetime) -> Optional[ActiveSchedule]:
        """Resolve completed data projected onto the current cycle."""
        return self.data_mgr.get_active(self._get_schedule_owner_date(now))

    def _resolve_cycle(self, owner_date: date) -> list[ResolvedPeriod]:
        """Resolve a cycle against the following effective schedule."""
        active = self.data_mgr.get_active(owner_date)
        next_active = self.data_mgr.get_active(owner_date + timedelta(days=1))
        if not active:
            return []
        try:
            return resolve_schedule_periods(
                active, self._parse_schedule_time(), next_active
            )
        except ValueError as exc:
            logger.warning(
                f"[BusySchedule] Failed to resolve cycle {owner_date}: {exc}"
            )
            return []

    def get_current_busy_period(
        self, now: datetime
    ) -> Optional[ResolvedPeriod]:
        """Return the current busy period on the absolute timeline."""
        owner_date = self._get_schedule_owner_date(now)
        for cycle_date in (owner_date - timedelta(days=1), owner_date):
            for resolved in self._resolve_cycle(cycle_date):
                if resolved.period.is_busy and resolved.contains(now):
                    return resolved
        return None

    def get_next_busy_period(self, now: datetime) -> Optional[ResolvedPeriod]:
        """Return the next busy period across the current and next cycle."""
        owner_date = self._get_schedule_owner_date(now)
        candidates = []
        for cycle_date in (owner_date, owner_date + timedelta(days=1)):
            candidates.extend(
                resolved
                for resolved in self._resolve_cycle(cycle_date)
                if resolved.period.is_busy and resolved.start > now
            )
        return min(candidates, key=lambda item: item.start) if candidates else None

    async def check_and_update_state(self):
        """Check current time and update busy state accordingly."""
        now = datetime.now()
        in_cooldown = self._is_in_wakeup_cooldown(now)
        current = self.get_current_busy_period(now)

        if current and not self._is_busy:
            if not in_cooldown and self._can_enter_busy(now):
                await self._enter_busy(current)
        elif current and self._is_busy:
            active_range = self._current_resolved_period
            if (
                not self._is_manual_period
                and active_range != current
            ):
                self._current_busy_period = current.period
                self._current_resolved_period = current
                self._current_busy_owner_date = current.owner_date
                self._current_busy_schedule_time = self._parse_schedule_time()
                logger.info(
                    f"[BusySchedule] Busy activity changed: "
                    f"{current.period.activity}"
                )
        elif not current and self._is_busy:
            if (
                self._is_manual_period
                and self._current_resolved_period
                and self._current_resolved_period.contains(now)
            ):
                logger.debug(
                    f"[BusySchedule] Still in manual period "
                    f"{self._current_busy_period.start_time}-"
                    f"{self._current_busy_period.end_time}, not exiting"
                )
                return
            logger.info(
                f"[BusySchedule] No active period found (current_period=None, "
                f"manual={self._current_busy_period is not None}), forcing exit busy"
            )
            await self._exit_busy()

    async def _enter_busy(
        self,
        period: BusyPeriod | ResolvedPeriod,
        owner_date: Optional[date] = None,
    ):
        """Enter busy state and retain its resolved absolute range."""
        resolved = period if isinstance(period, ResolvedPeriod) else None
        raw_period = resolved.period if resolved else period
        effective_owner = owner_date or self._get_schedule_owner_date(datetime.now())
        if resolved is None and raw_period.end_time:
            try:
                start, end = raw_period.to_absolute_datetimes(
                    effective_owner, *self._parse_schedule_time()
                )
                resolved = ResolvedPeriod(
                    effective_owner, raw_period, start, end
                )
            except (TypeError, ValueError):
                resolved = None
        self._is_busy = True
        self._current_busy_period = raw_period
        self._current_resolved_period = resolved
        self._is_manual_period = not isinstance(period, ResolvedPeriod)
        self._current_busy_owner_date = (
            resolved.owner_date
            if resolved
            else effective_owner
        )
        self._current_busy_schedule_time = self._parse_schedule_time()
        self._busy_start_time = datetime.now()
        logger.info(f"[BusySchedule] Entering busy state: {raw_period.activity}")

        if self._on_enter_busy:
            await self._on_enter_busy(raw_period)

    async def _exit_busy(self):
        """Exit busy state."""
        if self._current_busy_period:
            logger.info(f"[BusySchedule] Exiting busy state: {self._current_busy_period.activity}")

        self._is_busy = False
        exiting_period = self._current_busy_period
        self._current_busy_period = None
        self._current_resolved_period = None
        self._is_manual_period = False
        self._current_busy_owner_date = None
        self._current_busy_schedule_time = None
        self._busy_start_time = None

        if self._on_exit_busy and exiting_period:
            await self._on_exit_busy(exiting_period)

    async def wake_up(self, reason: str = "keyword"):
        """Wake up AI from busy state (e.g., by keyword trigger)."""
        if not self._is_busy:
            return

        self._wakeup_time = datetime.now()
        logger.info(f"[BusySchedule] AI woken up by {reason}")

        await self._exit_busy()