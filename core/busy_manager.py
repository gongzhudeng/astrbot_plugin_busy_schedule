"""Busy period manager - handles busy state, sleep, and chat protection."""

from datetime import datetime, date, timedelta
from typing import Optional
from astrbot.api import logger

from .data import ScheduleData, BusyPeriod, ScheduleDataManager


class BusyPeriodManager:
    """Manages busy periods and AI availability state."""

    def __init__(self, config: dict, data_mgr: ScheduleDataManager):
        self.config = config
        self.data_mgr = data_mgr

        # State tracking
        self._is_busy: bool = False
        self._current_busy_period: Optional[BusyPeriod] = None
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
        protect_minutes = self.config.get("chat_protect_minutes", 10)

        if self._last_user_message_time:
            inactive_minutes = (now - self._last_user_message_time).total_seconds() / 60
            return inactive_minutes >= protect_minutes

        return True

    def _is_in_wakeup_cooldown(self, now: datetime) -> bool:
        """Check if AI is in wakeup cooldown period."""
        if not self._wakeup_time:
            return False

        cooldown_minutes = self.config.get("wake_cooldown_minutes", 15)
        elapsed = (now - self._wakeup_time).total_seconds() / 60
        return elapsed < cooldown_minutes

    def get_current_busy_period(self, now: datetime) -> Optional[BusyPeriod]:
        """Get the busy period that contains the current time."""
        today = now.date()
        data = self.data_mgr.get(today)

        if not data or not data.busy_periods:
            return None

        for period in data.busy_periods:
            if period.is_busy and period.contains(now):
                return period

        return None

    def get_next_busy_period(self, now: datetime) -> Optional[BusyPeriod]:
        """Get the next upcoming busy period."""
        today = now.date()
        data = self.data_mgr.get(today)

        if not data or not data.busy_periods:
            return None

        for period in sorted(data.busy_periods, key=lambda p: p.start_time):
            if period.is_busy and period.start_datetime > now:
                return period

        return None

    async def check_and_update_state(self):
        """Check current time and update busy state accordingly."""
        now = datetime.now()
        in_cooldown = self._is_in_wakeup_cooldown(now)

        # Check if should be busy
        current_period = self.get_current_busy_period(now)

        if current_period and not self._is_busy:
            # Should enter busy — but respect cooldown and chat protection
            if not in_cooldown and self._can_enter_busy(now):
                await self._enter_busy(current_period)
        elif not current_period and self._is_busy:
            # Should exit busy — always allow exit regardless of cooldown
            if self._current_busy_period and self._current_busy_period.contains(now):
                logger.debug(
                    f"[BusySchedule] Still in manual period "
                    f"{self._current_busy_period.start_time}-{self._current_busy_period.end_time}, "
                    f"not exiting"
                )
                return  # Still within manual busy period, do not exit
            logger.info(
                f"[BusySchedule] No active period found (current_period=None, "
                f"manual={self._current_busy_period is not None}), forcing exit busy"
            )
            await self._exit_busy()

    async def _enter_busy(self, period: BusyPeriod):
        """Enter busy state."""
        self._is_busy = True
        self._current_busy_period = period
        self._busy_start_time = datetime.now()
        logger.info(f"[BusySchedule] Entering busy state: {period.activity}")

        if self._on_enter_busy:
            await self._on_enter_busy(period)

    async def _exit_busy(self):
        """Exit busy state."""
        if self._current_busy_period:
            logger.info(f"[BusySchedule] Exiting busy state: {self._current_busy_period.activity}")

        self._is_busy = False
        exiting_period = self._current_busy_period
        self._current_busy_period = None
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