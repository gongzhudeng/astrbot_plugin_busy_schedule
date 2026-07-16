"""AI Busy Schedule Plugin - Let AI have a real life rhythm."""

import asyncio
import random
import re
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Plain
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_tools import StarTools

from .core.data import (
    ActiveSchedule,
    ResolvedPeriod,
    ScheduleDataManager,
    ScheduleData,
    BusyPeriod,
    get_schedule_owner_date,
    parse_clock_time,
    parse_schedule_time,
    resolve_schedule_periods,
)
from .core.generator import (
    DeterministicScheduleError,
    ScheduleGenerator,
    _SCHEMA_DEFAULTS,
)
from .core.busy_manager import BusyPeriodManager
from .core.message_interceptor import MessageInterceptor
from .core.prompt_injector import PromptInjector


@register(
    "astrbot_plugin_busy_schedule",
    "灵犀 · AI忙碌时段管理",
    "让AI拥有真实的生活节奏！自动计算忙碌时段、智能拦截合并消息、特殊关键词唤醒",
    "v1.3.6",
    "https://github.com/gongzhudeng/astrbot_plugin_busy_schedule",
)
class BusySchedulePlugin(Star):
    """Main plugin class for AI busy schedule management."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = StarTools.get_data_dir()

        # Data files
        self.schedule_data_file = self.data_dir / "schedule_data.json"

        # Core modules (initialized in initialize())
        self.data_mgr: Optional[ScheduleDataManager] = None
        self.generator: Optional[ScheduleGenerator] = None
        self.busy_mgr: Optional[BusyPeriodManager] = None
        self.interceptor: Optional[MessageInterceptor] = None
        self.injector: Optional[PromptInjector] = None

        # Background tasks
        self._state_check_task: Optional[asyncio.Task] = None
        self._schedule_gen_task: Optional[asyncio.Task] = None
        self._daily_refresh_task: Optional[asyncio.Task] = None

        self._last_refresh_owner_date: Optional[date] = None
        self._refresh_retry_owner_date: Optional[date] = None
        self._refresh_retry_after: Optional[datetime] = None

        # Peek state: probability stays latched until its delivery transaction finishes
        self._peek_timers: dict[str, asyncio.Task] = {}
        self._peek_latched: set[str] = set()

        # One queued-message delivery transaction per user
        self._delivery_tasks: dict[str, asyncio.Task] = {}
        self._delivery_locks: dict[str, asyncio.Lock] = {}
        self._suppress_exit_delivery = False

        # Periodic poll task: background loop that fires while busy
        self._busy_poll_task: Optional[asyncio.Task] = None

        # Target umo for daily schedule generation (persisted across restarts)
        self._schedule_target_umo: Optional[str] = None
        self._state_file: Optional[Path] = None

    async def initialize(self):
        """Initialize plugin and all modules."""
        logger.info("[BusySchedule] Initializing plugin...")

        # Initialize modules
        self._state_file = self.data_dir / "plugin_state.json"
        self._load_state()
        self.data_mgr = ScheduleDataManager(self.schedule_data_file)
        self.generator = ScheduleGenerator(self.context, self.config, self.data_mgr)
        self.busy_mgr = BusyPeriodManager(self.config, self.data_mgr)
        self.interceptor = MessageInterceptor(self.config)
        self.injector = PromptInjector(self.config)

        # Set callbacks
        self.busy_mgr.set_callbacks(
            on_enter_busy=self._on_enter_busy,
            on_exit_busy=self._on_exit_busy,
        )

        # Reset the flag so downstream plugins never see a stale value from a previous run
        self.context._busy_schedule_is_busy = False
        self.context._busy_schedule_today_schedule = ""

        # Expose a force-check callable so downstream plugins can get an immediate state refresh
        self.context._busy_schedule_force_check = self.busy_mgr.check_and_update_state

        # Expose wake-and-flush for Spark: wake AI from busy and send queued messages first
        async def _wake_and_flush(umo: str):
            period = self.busy_mgr._current_busy_period
            has_queue = self.interceptor.has_queued_messages(umo)
            if self.busy_mgr.is_busy and period and period.is_sleep:
                return
            if self.busy_mgr.is_busy:
                if has_queue and period:
                    await self._deliver_queued_messages(umo, period, "external")
                await self.busy_mgr.wake_up("external")
            elif has_queue:
                fallback_period = period or BusyPeriod(
                    start_time="??:??",
                    end_time=datetime.now().strftime("%H:%M"),
                    activity="忙碌时段",
                )
                await self._deliver_queued_messages(umo, fallback_period, "external")

        self.context._busy_schedule_wake_and_flush = _wake_and_flush
        self.context._busy_schedule_get_timeline = self._export_timeline
        logger.info(
            "[BusySchedule] Structured timeline interface registered "
            f"(owner_date={self._get_effective_date().isoformat()})"
        )

        # Start background tasks
        self._state_check_task = asyncio.create_task(self._state_check_loop())

        # Schedule generation as background task to avoid blocking initialization
        self._schedule_gen_task = asyncio.create_task(self._ensure_today_schedule_async())

        # Daily refresh loop - regenerate schedule at schedule_time each day
        self._daily_refresh_task = asyncio.create_task(self._daily_refresh_loop())

        logger.info("[BusySchedule] Plugin initialized successfully")

    async def terminate(self):
        """Cleanup when plugin is unloaded."""
        logger.info("[BusySchedule] Terminating plugin...")

        # Cancel background tasks
        if self._state_check_task:
            self._state_check_task.cancel()
        if self._schedule_gen_task:
            self._schedule_gen_task.cancel()
        if self._daily_refresh_task:
            self._daily_refresh_task.cancel()

        # Cancel all automatic delivery tasks
        for task in list(self._peek_timers.values()):
            task.cancel()
        self._peek_timers.clear()
        self._peek_latched.clear()
        for task in list(self._delivery_tasks.values()):
            task.cancel()
        self._delivery_tasks.clear()
        self._delivery_locks.clear()

        # Cancel poll task
        if self._busy_poll_task and not self._busy_poll_task.done():
            self._busy_poll_task.cancel()
        self._busy_poll_task = None

        if getattr(self.context, "_busy_schedule_get_timeline", None) == self._export_timeline:
            delattr(self.context, "_busy_schedule_get_timeline")

        logger.info("[BusySchedule] Plugin terminated")

    def _disable_cycle_retries(self, owner_date: date, error: Exception) -> None:
        """Mark a deterministic protocol failure as handled for this cycle."""
        self._last_refresh_owner_date = owner_date
        self._refresh_retry_owner_date = None
        self._refresh_retry_after = None
        logger.error(
            f"[BusySchedule] Schedule protocol failed for {owner_date}; "
            f"automatic retries disabled for this cycle: {error}"
        )

    async def _ensure_today_schedule_async(self):
        """Async wrapper for schedule generation with error handling."""
        try:
            await self._ensure_today_schedule()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[BusySchedule] Async schedule generation failed: {e}")

    async def _ensure_today_schedule(self):
        """Ensure the current schedule cycle has completed data."""
        owner_date = self._get_effective_date()
        data = self.data_mgr.get(owner_date)

        if data and data.status == "completed":
            self._last_refresh_owner_date = owner_date
        else:
            logger.info(f"[BusySchedule] Generating schedule cycle {owner_date}...")
            try:
                await self.generator.generate_schedule_or_wait(
                    owner_date, umo=self._schedule_target_umo
                )
                self._last_refresh_owner_date = owner_date
                self._refresh_retry_owner_date = None
                self._refresh_retry_after = None
            except DeterministicScheduleError as e:
                self._disable_cycle_retries(owner_date, e)
            except Exception as e:
                self._refresh_retry_owner_date = owner_date
                self._refresh_retry_after = datetime.now() + timedelta(minutes=5)
                logger.error(f"[BusySchedule] Failed to generate schedule: {e}")

        self._sync_schedule_to_context()

    def _get_active_schedule(
        self, now: Optional[datetime] = None
    ) -> Optional[ActiveSchedule]:
        """Return completed data projected onto the current owner cycle."""
        current = now or datetime.now()
        owner_date = get_schedule_owner_date(
            current, parse_schedule_time(self._get_config("schedule_time", "07:00"))
        )
        return self.data_mgr.get_active(owner_date)

    def _get_resolved_timeline(
        self, owner_date: date, include_previous_sleep: bool = True
    ) -> list[ResolvedPeriod]:
        """Resolve current activities and any sleep carried over at the boundary."""
        schedule_time = parse_schedule_time(
            self._get_config("schedule_time", "07:00")
        )
        resolved = []
        cycle_dates = (
            (owner_date - timedelta(days=1), owner_date)
            if include_previous_sleep
            else (owner_date,)
        )
        for cycle_date in cycle_dates:
            active = self.data_mgr.get_active(cycle_date)
            next_active = self.data_mgr.get_active(cycle_date + timedelta(days=1))
            if not active:
                continue
            try:
                periods = resolve_schedule_periods(
                    active, schedule_time, next_active
                )
            except ValueError as exc:
                logger.warning(
                    f"[BusySchedule] Failed to resolve cycle {cycle_date}: {exc}"
                )
                continue
            if cycle_date == owner_date - timedelta(days=1):
                periods = [item for item in periods if item.period.is_open_sleep]
            resolved.extend(periods)
        return sorted(resolved, key=lambda item: item.start)

    def _export_timeline(self, owner_date: Optional[date] = None) -> list[dict]:
        """Return a framework-neutral schedule timeline for downstream plugins."""
        target_date = owner_date or self._get_effective_date()
        active = self.data_mgr.get_active(target_date)
        if not active:
            return []

        resolved_by_period = {
            id(item.period): item for item in self._get_resolved_timeline(target_date)
        }
        schedule_time = parse_schedule_time(
            self._get_config("schedule_time", "07:00")
        )
        timeline = []
        sleep_keywords = ("睡觉", "睡眠", "就寝", "入睡", "午睡", "小睡", "休眠")
        periods = active.data.busy_periods
        for index, period in enumerate(periods):
            resolved = resolved_by_period.get(id(period))
            start = resolved.start if resolved else None
            end = resolved.end if resolved else None
            valid = True
            error = ""
            if start is None:
                if period.is_open_sleep:
                    hour, minute = parse_clock_time(period.start_time)
                    start_date = (
                        active.owner_date + timedelta(days=1)
                        if (hour, minute) < schedule_time
                        else active.owner_date
                    )
                    start = datetime.combine(start_date, datetime.min.time()).replace(
                        hour=hour, minute=minute
                    )
                else:
                    try:
                        start, _ = period.to_absolute_datetimes(
                            active.owner_date,
                            *schedule_time,
                            resolved_end=end,
                        )
                    except ValueError as exc:
                        valid = False
                        error = str(exc)
            inferred_open_sleep = (
                period.is_open_sleep
                and index == len(periods) - 1
                and any(keyword in period.activity for keyword in sleep_keywords)
            )
            if period.end_time is None and not inferred_open_sleep:
                valid = False
                end = None
                error = "ordinary activity is missing end_time"
            elif inferred_open_sleep and resolved is None:
                end = None
                error = "sleep end is unavailable until the next schedule exists"

            timeline.append(
                {
                    "owner_date": active.owner_date.isoformat(),
                    "activity": period.activity,
                    "period_type": period.period_type,
                    "start": start,
                    "end": end,
                    "valid": valid,
                    "error": error,
                }
            )
        return timeline

    def _sync_schedule_to_context(self):
        """Sync active schedule data to context for downstream plugins."""
        custom_prompt = self._get_config("custom_prompt", "")
        self.context._busy_schedule_custom_prompt = custom_prompt or ""
        now = datetime.now()
        active = self._get_active_schedule(now)
        if active:
            data = active.data
            timeline = self._get_resolved_timeline(active.owner_date)
            self.context._busy_schedule_today_schedule = data.schedule
            self.context._busy_schedule_outfit = data.outfit or ""
            current = self.injector._find_current_activity(timeline, now)
            self.context._busy_schedule_current_activity = current or ""
            candidates = [item for item in timeline if item.start > now]
            if candidates:
                resolved = min(candidates, key=lambda item: item.start)
                self.context._busy_schedule_next_activity = (
                    f"{resolved.period.activity}"
                    f"（{resolved.start.strftime('%H:%M')}开始）"
                )
            else:
                self.context._busy_schedule_next_activity = ""
        else:
            self.context._busy_schedule_today_schedule = ""
            self.context._busy_schedule_outfit = ""
            self.context._busy_schedule_current_activity = ""
            self.context._busy_schedule_next_activity = ""

    def _get_effective_date(self) -> date:
        """Return the schedule-cycle date for display/injection.

        Delegates to BusyPeriodManager._get_schedule_owner_date() so the same
        schedule_time boundary is used everywhere in the plugin.
        """
        return get_schedule_owner_date(
            datetime.now(),
            parse_schedule_time(self._get_config("schedule_time", "07:00")),
        )

    async def _daily_refresh_loop(self):
        """Refresh once whenever the configured schedule cycle changes."""
        while True:
            try:
                await asyncio.sleep(30)
                now = datetime.now()
                owner_date = self._get_effective_date()
                if owner_date == self._last_refresh_owner_date:
                    continue
                if (
                    owner_date == self._refresh_retry_owner_date
                    and self._refresh_retry_after
                    and now < self._refresh_retry_after
                ):
                    continue

                data = self.data_mgr.get(owner_date)
                if data and data.status == "completed":
                    self._last_refresh_owner_date = owner_date
                    self._refresh_retry_owner_date = None
                    self._refresh_retry_after = None
                    self._sync_schedule_to_context()
                    continue

                logger.info(f"[BusySchedule] Refreshing schedule cycle {owner_date}")
                try:
                    await self.generator.generate_schedule_or_wait(
                        owner_date, umo=self._schedule_target_umo
                    )
                    self._last_refresh_owner_date = owner_date
                    self._refresh_retry_owner_date = None
                    self._refresh_retry_after = None
                    logger.info(
                        f"[BusySchedule] Schedule cycle {owner_date} refreshed"
                    )
                except DeterministicScheduleError as e:
                    self._disable_cycle_retries(owner_date, e)
                except Exception as e:
                    self._refresh_retry_owner_date = owner_date
                    self._refresh_retry_after = now + timedelta(minutes=5)
                    logger.error(f"[BusySchedule] Daily refresh failed: {e}")

                self._sync_schedule_to_context()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[BusySchedule] Daily refresh loop error: {e}")
                await asyncio.sleep(60)

    async def _state_check_loop(self):
        """Background loop to check and update busy state."""
        while True:
            try:
                await asyncio.sleep(30)  # Check every 30 seconds
                await self.busy_mgr.check_and_update_state()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[BusySchedule] State check error: {e}")
                await asyncio.sleep(60)
            finally:
                # Always sync the flag so downstream plugins never see a stale value
                self.context._busy_schedule_is_busy = self.busy_mgr.is_busy
                self._sync_schedule_to_context()
                self._reconcile_automatic_tasks()
                if self.busy_mgr.is_busy:
                    logger.info(
                        f"[BusySchedule] State sync: is_busy=True, "
                        f"manual_period={self.busy_mgr._current_busy_period is not None}, "
                        f"cooldown={self.busy_mgr._is_in_wakeup_cooldown(datetime.now())}"
                    )

    def _current_period(self) -> Optional[BusyPeriod]:
        return self.busy_mgr._current_busy_period

    def _is_sleeping(self) -> bool:
        period = self._current_period()
        return bool(self.busy_mgr.is_busy and period and period.is_sleep)

    def _reconcile_automatic_tasks(self):
        """Align automatic tasks with the current structural busy period."""
        period = self._current_period()
        if not self.busy_mgr.is_busy or not period or period.is_sleep:
            if self._busy_poll_task and not self._busy_poll_task.done():
                self._busy_poll_task.cancel()
            self._busy_poll_task = None
            if period and period.is_sleep:
                for user_id in list(self._peek_timers):
                    self._cancel_peek_timer(user_id, clear_latch=True)
            return

        if self._get_config("poll_enabled", False) and (
            not self._busy_poll_task or self._busy_poll_task.done()
        ):
            self._busy_poll_task = asyncio.create_task(self._busy_poll_loop(period))

    @staticmethod
    def _normalized_range(first: object, second: object) -> tuple[float, float]:
        try:
            lower = max(0.0, float(first))
        except (TypeError, ValueError):
            lower = 0.0
        try:
            upper = max(0.0, float(second))
        except (TypeError, ValueError):
            upper = lower
        return (lower, upper) if lower <= upper else (upper, lower)

    @staticmethod
    def _normalized_probability(value: object) -> float:
        try:
            return min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    async def _on_enter_busy(self, period: BusyPeriod):
        """Callback when entering busy state."""
        logger.info(f"[BusySchedule] Entering busy: {period.activity}")
        self.context._busy_schedule_is_busy = True

        if period.is_sleep:
            for user_id in list(self._peek_timers):
                self._cancel_peek_timer(user_id, clear_latch=True)
            return

        if self._get_config("poll_enabled", False):
            if self._busy_poll_task and not self._busy_poll_task.done():
                self._busy_poll_task.cancel()
            self._busy_poll_task = asyncio.create_task(self._busy_poll_loop(period))

    async def _on_exit_busy(self, period: BusyPeriod):
        """Callback when exiting busy state."""
        logger.info(f"[BusySchedule] Exiting busy: {period.activity}")
        self.context._busy_schedule_is_busy = False

        if self._busy_poll_task and not self._busy_poll_task.done():
            self._busy_poll_task.cancel()
        self._busy_poll_task = None

        if self._suppress_exit_delivery:
            return

        for user_id in self.interceptor.get_all_queued_user_ids():
            if not self.interceptor.has_queued_messages(user_id):
                continue
            if user_id in self._peek_latched:
                continue
            self._schedule_delivery(
                user_id,
                period,
                reason="exit",
                delay_range=self._normalized_range(
                    self._get_config("exit_delay_min_seconds", 10),
                    self._get_config("exit_delay_max_seconds", 120),
                ),
            )

    def _schedule_delivery(
        self,
        user_id: str,
        period: BusyPeriod,
        reason: str,
        delay_range: tuple[float, float] = (0.0, 0.0),
    ) -> asyncio.Task:
        current = self._delivery_tasks.get(user_id)
        if current and not current.done():
            if reason == "peek":
                current.add_done_callback(
                    lambda _task: self._peek_latched.discard(user_id)
                )
            return current

        async def _run():
            try:
                delay = random.uniform(*delay_range)
                if delay > 0:
                    logger.info(
                        f"[BusySchedule] Delivery delay for {user_id}: {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                await self._deliver_queued_messages(user_id, period, reason)
            finally:
                if self._delivery_tasks.get(user_id) is asyncio.current_task():
                    self._delivery_tasks.pop(user_id, None)
                if reason == "peek":
                    self._peek_latched.discard(user_id)

        task = asyncio.create_task(_run())
        self._delivery_tasks[user_id] = task
        return task

    async def _deliver_queued_messages(
        self, user_id: str, period: BusyPeriod, reason: str
    ) -> bool:
        lock = self._delivery_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            if not self.interceptor.has_queued_messages(user_id):
                return False

            guarded_delivery = reason in {"peek", "poll", "max_count", "exit"}
            if guarded_delivery:
                await self.busy_mgr.check_and_update_state()
            current_period = self._current_period()
            if guarded_delivery and self._is_sleeping():
                logger.info(
                    f"[BusySchedule] Delivery cancelled during sleep: {user_id}"
                )
                return False

            automatic_wake = reason in {"peek", "poll", "max_count"}
            if automatic_wake and self.busy_mgr.is_busy:
                if not current_period:
                    return False
                period = current_period
                self.busy_mgr.update_last_message_time()
                self._suppress_exit_delivery = True
                try:
                    await self.busy_mgr.wake_up(reason)
                finally:
                    self._suppress_exit_delivery = False

            return await self._send_merged_messages(user_id, period)

    def _start_peek_timer(self, user_id: str, period: BusyPeriod):
        """Start or reset the latched peek countdown for one user."""
        self._cancel_peek_timer(user_id, clear_latch=False)
        self._peek_latched.add(user_id)
        delay_range = self._normalized_range(
            self._get_config("peek_delay_min_seconds", 5),
            self._get_config("peek_delay_max_seconds", 30),
        )
        delay = random.uniform(*delay_range)
        logger.info(f"[BusySchedule] Peek timer started for {user_id}: {delay:.1f}s")

        async def _callback():
            try:
                await asyncio.sleep(delay)
                if self._is_sleeping():
                    return
                self._schedule_delivery(user_id, period, reason="peek")
            except asyncio.CancelledError:
                raise
            finally:
                if self._peek_timers.get(user_id) is asyncio.current_task():
                    self._peek_timers.pop(user_id, None)
                if self._is_sleeping():
                    self._peek_latched.discard(user_id)

        self._peek_timers[user_id] = asyncio.create_task(_callback())

    def _cancel_peek_timer(self, user_id: str, clear_latch: bool = False):
        """Cancel a peek countdown without reopening probability unless requested."""
        task = self._peek_timers.pop(user_id, None)
        if task and not task.done():
            task.cancel()
        if clear_latch:
            self._peek_latched.discard(user_id)

    def _get_poll_params(self, activity: str) -> tuple[float, float, float]:
        """Return (probability, min_minutes, max_minutes) for the given activity.

        Checks poll_activity_rules for a matching keyword first; falls back to
        global poll_probability / poll_interval_min/max_minutes.
        Rule format: 'keyword:probability:min-max'  e.g. '洗澡:0.02:20-45'
        """
        rules = self._get_config("poll_activity_rules", [])
        for rule in rules:
            rule = str(rule).strip()
            if not rule:
                continue
            parts = rule.split(":")
            if len(parts) != 3:
                continue
            keyword, prob_str, interval_str = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if keyword and keyword in activity:
                try:
                    prob = self._normalized_probability(prob_str)
                    mn, mx = interval_str.split("-")
                    mn_value, mx_value = self._normalized_range(mn, mx)
                    return prob, mn_value, mx_value
                except Exception:
                    continue
        # Global defaults
        prob = self._normalized_probability(
            self._get_config("poll_probability", 0.3)
        )
        mn, mx = self._normalized_range(
            self._get_config("poll_interval_min_minutes", 5),
            self._get_config("poll_interval_max_minutes", 15),
        )
        return prob, mn, mx

    async def _busy_poll_loop(self, period: BusyPeriod):
        """Background loop that fires periodically while busy and may send queued messages."""
        try:
            while self.busy_mgr.is_busy and not self._is_sleeping():
                activity = self.busy_mgr.current_activity or ""
                prob, mn_min, mx_min = self._get_poll_params(activity)
                wait_seconds = random.uniform(mn_min * 60, mx_min * 60)
                logger.info(
                    f"[BusySchedule] Poll loop: next check in {wait_seconds:.0f}s "
                    f"(activity={activity!r}, prob={prob})"
                )
                await asyncio.sleep(wait_seconds)

                if not self.busy_mgr.is_busy or self._is_sleeping():
                    break

                if random.random() >= prob:
                    continue

                # Triggered — send queued messages for all users (skip if peek already handling)
                _, quiet = self._normalized_range(
                    self._get_config("poll_quiet_seconds", 30), 0
                )
                user_ids = self.interceptor.get_all_queued_user_ids()
                for user_id in user_ids:
                    if not self.interceptor.has_queued_messages(user_id):
                        continue
                    if user_id in self._peek_timers and not self._peek_timers[user_id].done():
                        continue
                    # Quiet period check: skip if user sent a message recently
                    if quiet > 0:
                        queue_msgs = self.interceptor.get_queued_messages(user_id)
                        if queue_msgs:
                            newest = max(
                                datetime.fromisoformat(m["timestamp"]) for m in queue_msgs
                            )
                            if (datetime.now() - newest).total_seconds() < quiet:
                                logger.info(
                                    f"[BusySchedule] Poll skipped for {user_id}: quiet period active"
                                )
                                continue
                    current_period = self._current_period() or period
                    if current_period.is_sleep:
                        break
                    logger.info(f"[BusySchedule] Poll triggered send for {user_id}")
                    self._schedule_delivery(user_id, current_period, reason="poll")
        except asyncio.CancelledError:
            pass

    async def _send_merged_messages(
        self, user_id: str, period: BusyPeriod
    ) -> bool:
        """Re-inject one merged queue and commit it only after enqueue succeeds."""
        merged_text = self.interceptor.get_merged_message(
            user_id,
            period.start_time,
            period.end_time or datetime.now().strftime("%H:%M"),
        )

        if not merged_text:
            return False

        extra_components = self.interceptor.get_extra_components(user_id)

        # Get stored event reference
        events = self.interceptor._event_refs.get(user_id, [])
        if not events:
            logger.warning(
                f"[BusySchedule] No event ref for {user_id}, cannot send merged messages"
            )
            return False

        # Use the last event as template
        last_event = events[-1]

        # Prepend wake_prefix so WakingCheckStage re-evaluates is_wake=True
        # (it ignores pre-set is_wake and recalculates from scratch each time)
        wake_prefixes = self.context.get_config().get("wake_prefix", ["/"])
        wake_prefix = wake_prefixes[0] if wake_prefixes else "/"
        prefixed_text = wake_prefix + merged_text

        # Build a clean event instead of reusing or deep-copying a stopped one
        reinjected_message = last_event.message_obj.__class__()
        reinjected_message.__dict__.update(last_event.message_obj.__dict__)
        reinjected_message.type = last_event.get_message_type()
        reinjected_message.message_str = prefixed_text
        reinjected_message.raw_message = getattr(last_event.message_obj, "raw_message", None)
        reinjected_message.self_id = last_event.get_self_id()
        reinjected_message.sender = last_event.message_obj.sender
        reinjected_message.group = getattr(last_event.message_obj, "group", None)
        reinjected_message.session_id = last_event.session_id
        reinjected_message.message_id = getattr(last_event.message_obj, "message_id", None)
        if hasattr(reinjected_message, "message"):
            reinjected_message.message = [Plain(prefixed_text)] + extra_components

        event_kwargs = {
            "message_str": prefixed_text,
            "message_obj": reinjected_message,
            "platform_meta": last_event.platform_meta,
            "session_id": last_event.session_id,
        }
        if hasattr(last_event, "bot"):
            event_kwargs["bot"] = last_event.bot
        if hasattr(last_event, "client"):
            event_kwargs["client"] = last_event.client
        if hasattr(last_event, "interaction_followup_webhook"):
            event_kwargs["interaction_followup_webhook"] = last_event.interaction_followup_webhook

        reinjected_event = last_event.__class__(**event_kwargs)

        # Preserve only the message identity and clear runtime state
        reinjected_event.role = "member"
        reinjected_event.is_at_or_wake_command = False
        reinjected_event.is_wake = False
        reinjected_event._force_stopped = False
        reinjected_event._result = None
        reinjected_event._has_send_oper = False
        reinjected_event.call_llm = False
        reinjected_event.plugins_name = None
        reinjected_event._extras = {}
        reinjected_event._temporary_local_files = []
        reinjected_event.platform = last_event.platform_meta

        # Mark as busy_schedule merged to prevent re-interception
        reinjected_event.set_extra("busy_schedule_merged", True)
        # Also mark as chat_merger merged so chat_merger does not re-intercept
        reinjected_event.set_extra("chat_merger_merged", True)

        logger.info(
            f"[BusySchedule] Sending merged messages for {user_id}: "
            f"{len(merged_text)} chars, content: {merged_text[:80]}..."
        )

        try:
            self.context.get_event_queue().put_nowait(reinjected_event)
        except Exception as e:
            logger.error(f"[BusySchedule] Failed to re-inject event for {user_id}: {e}")
            return False

        self.interceptor.mark_sent(user_id)
        return True

    def _check_wake_keywords(self, message_text: str) -> bool:
        """Check if message contains wake keywords."""
        keywords = self._get_config("wake_keywords", ["咋不回我", "快点回我呀"])
        match_mode = self._get_config("keyword_match_mode", "包含关键词模式")

        if not keywords:
            return False

        for keyword in keywords:
            if match_mode == "完全匹配模式":
                if message_text.strip() == keyword:
                    return True
            else:  # 包含关键词模式
                if keyword in message_text:
                    return True

        return False

    def _check_filter_keywords(self, message_text: str) -> bool:
        """Check if message matches busy filter keywords (silently drop during busy)."""
        keywords = self._get_config("busy_filter_keywords", [])
        if not keywords:
            return False
        match_mode = self._get_config("busy_filter_keyword_match_mode", "包含关键词模式")
        for keyword in keywords:
            if match_mode == "完全匹配模式":
                if message_text.strip() == keyword:
                    return True
            else:  # 包含关键词模式
                if keyword in message_text:
                    return True
        return False

    def _load_state(self):
        """Load persisted plugin state (e.g. schedule_target_umo)."""
        if not self._state_file or not self._state_file.exists():
            return
        try:
            import json
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            self._schedule_target_umo = data.get("schedule_target_umo") or None
            if self._schedule_target_umo:
                logger.info(f"[BusySchedule] Loaded schedule_target_umo: {self._schedule_target_umo}")
        except Exception as e:
            logger.warning(f"[BusySchedule] Failed to load plugin state: {e}")

    def _save_state(self):
        """Persist plugin state to disk."""
        if not self._state_file:
            return
        try:
            import json
            data = {"schedule_target_umo": self._schedule_target_umo or ""}
            self._state_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[BusySchedule] Failed to save plugin state: {e}")

    def _get_config(self, key: str, default=None):
        """Get config value with schema default fallback."""
        # Nested groups take priority — user-edited values live here
        for group_name in [
            "基础设置",
            "忙碌时段",
            "随机接收",
            "定时检查",
            "关键词设置",
            "消息合并",
            "日程生成",
        ]:
            group = self.config.get(group_name, {})
            if isinstance(group, dict) and key in group:
                val = group[key]
                if val is not None and val != "" and val != {} and val != []:
                    return val

        # Flat key (may carry schema defaults merged by AstrBotConfig)
        value = self.config.get(key)
        if value is not None and value != "" and value != {} and value != []:
            return value

        # Fall back to schema defaults
        schema_default = _SCHEMA_DEFAULTS.get(key)
        if schema_default is not None:
            return schema_default

        return default

    # ==================== Event Handlers ====================

    @filter.event_message_type(EventMessageType.ALL, priority=10)
    async def on_message(self, event: AstrMessageEvent):
        """Handle incoming messages."""
        if not self._get_config("enabled", True):
            return

        # Skip events already processed by busy_schedule merge
        if event.get_extra("busy_schedule_merged", False):
            return

        message_text = event.message_str.strip()
        user_id = event.unified_msg_origin

        # Skip empty messages
        if not message_text:
            return

        # Detect slash commands via raw message chain (AstrBot may strip "/" from message_str)
        is_slash_command = False
        if hasattr(event.message_obj, 'message') and event.message_obj.message:
            for seg in event.message_obj.message:
                if hasattr(seg, 'text') and seg.text.strip().startswith("/"):
                    is_slash_command = True
                    break

        # Slash commands are not real chat - skip interception and chat protection
        if is_slash_command or message_text.startswith("/"):
            return

        # Check for wake keywords first
        if self._check_wake_keywords(message_text):
            extra_comps = [c for c in event.message_obj.message if not isinstance(c, Plain)] if hasattr(event.message_obj, "message") else []
            has_queue = self.interceptor.has_queued_messages(user_id)
            if self.busy_mgr.is_busy:
                self._cancel_peek_timer(user_id, clear_latch=True)
                self.interceptor.queue_message(
                    user_id,
                    message_text,
                    event,
                    extra_components=extra_comps,
                )
                self.busy_mgr.update_last_message_time()
                await self.busy_mgr.wake_up("keyword")
                event.stop_event()
                return
            elif has_queue:
                # state_check_loop may have already exited busy before the wake keyword arrived;
                # the queue is still non-empty so flush it now.
                now = datetime.now()
                period = self.busy_mgr._current_busy_period or BusyPeriod(
                    start_time="??:??", end_time=now.strftime("%H:%M"), activity="忙碌时段"
                )
                await self._send_merged_messages(user_id, period)
                event.stop_event()
                return
            else:
                # Not busy and no queue, let message through normally
                return

        # If busy, intercept the message (do NOT update chat protection)
        if self.busy_mgr.is_busy:
            # Check filter keywords - silently drop matching messages (not queued, not responded)
            if self._check_filter_keywords(message_text):
                event.stop_event()
                return

            extra_comps = [c for c in event.message_obj.message if not isinstance(c, Plain)] if hasattr(event.message_obj, "message") else []

            result = self.interceptor.queue_message(
                user_id,
                message_text,
                event,
                extra_components=extra_comps,
            )

            if result == "queued":
                event.stop_event()
                if (
                    not self._is_sleeping()
                    and self._get_config("peek_enabled", False)
                ):
                    period = self._current_period() or BusyPeriod(
                        start_time="??:??",
                        end_time=datetime.now().strftime("%H:%M"),
                        activity="忙碌时段",
                    )
                    if user_id in self._peek_latched:
                        if user_id in self._peek_timers:
                            self._start_peek_timer(user_id, period)
                    elif random.random() < self._normalized_probability(
                        self._get_config("peek_probability", 0.05)
                    ):
                        self._start_peek_timer(user_id, period)
            elif result == "force_send":
                event.stop_event()
                if not self._is_sleeping():
                    period = self._current_period() or BusyPeriod(
                        start_time="??:??",
                        end_time=datetime.now().strftime("%H:%M"),
                        activity="忙碌时段",
                    )
                    self._cancel_peek_timer(user_id, clear_latch=True)
                    self._schedule_delivery(user_id, period, reason="max_count")
            return

        # Not busy - update last message time for chat protection
        self.busy_mgr.update_last_message_time()

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """Inject prompts into LLM request.

        Injection structure (ordered by change frequency, low → high):
        1. Static part  (outfit + schedule)      → changes once per day, most cache-friendly
        2. Schedule part (current + next activity) → changes on activity transitions (~10-12x/day)
        3. Busy flag    (only when in busy mode)    → changes per request while busy

        Parts 1 & 2 are inserted together after persona prompt for optimal prompt cache.
        Part 3 is appended at the end only when AI is in busy state.
        """
        if not self._get_config("enabled", True):
            return

        # Record the umo for use in daily auto-generation
        umo = event.unified_msg_origin
        if umo and umo != self._schedule_target_umo:
            self._schedule_target_umo = umo
            self._save_state()

        now = datetime.now()
        active = self._get_active_schedule(now)
        if not active:
            return
        data = active.data
        timeline = self._get_resolved_timeline(active.owner_date)

        # Part 1 + 2: static (outfit+schedule) + semi-static (current/next activity)
        static_injection = self.injector.build_static_injection(data)
        schedule_injection = self.injector.build_schedule_injection(
            data, timeline, now
        )
        custom_injection = self.injector.build_custom_injection()

        cacheable_injection = static_injection
        if custom_injection:
            cacheable_injection += f"\n\n{custom_injection}"
        if schedule_injection:
            cacheable_injection += f"\n\n{schedule_injection}"

        # Part 3: busy flag (dynamic, only when busy)
        busy_injection = ""
        if self.busy_mgr.is_busy and self.busy_mgr.current_activity:
            period = BusyPeriod(
                start_time="",
                end_time="",
                activity=self.busy_mgr.current_activity,
            )
            busy_injection = self.injector.build_busy_state_injection(period)

        # Inject into system_prompt
        current_prompt = req.system_prompt or ""

        # Markers
        cache_marker = "<!-- BUSY_SCHEDULE_CACHE -->"
        cache_end_marker = "<!-- /BUSY_SCHEDULE_CACHE -->"
        busy_marker = "<!-- BUSY_SCHEDULE_BUSY -->"
        busy_end_marker = "<!-- /BUSY_SCHEDULE_BUSY -->"

        # Part 1+2: insert after persona prompt (cache-friendly block)
        if cache_marker in current_prompt:
            pattern = f"{re.escape(cache_marker)}.*?{re.escape(cache_end_marker)}"
            new_cache = f"{cache_marker}\n{cacheable_injection}\n{cache_end_marker}"
            current_prompt = re.sub(pattern, new_cache, current_prompt, flags=re.DOTALL)
        else:
            # No persona end marker - append at end (persona is at the beginning)
            # This ensures: persona → static → dynamic (at end)
            logger.info(f"[BusySchedule] Appending cache block at end (after persona)")
            current_prompt = f"{current_prompt}\n\n{cache_marker}\n{cacheable_injection}\n{cache_end_marker}"

        # Part 3: busy flag (append at end, only when busy)
        if busy_injection:
            if busy_marker in current_prompt:
                pattern = f"{re.escape(busy_marker)}.*?{re.escape(busy_end_marker)}"
                new_busy = f"{busy_marker}\n{busy_injection}\n{busy_end_marker}"
                current_prompt = re.sub(pattern, new_busy, current_prompt, flags=re.DOTALL)
            else:
                current_prompt += f"\n\n{busy_marker}\n{busy_injection}\n{busy_end_marker}"
        else:
            # Remove busy marker if no longer busy
            if busy_marker in current_prompt:
                pattern = f"\n*\s*{re.escape(busy_marker)}.*?{re.escape(busy_end_marker)}\n*"
                current_prompt = re.sub(pattern, "", current_prompt, flags=re.DOTALL)

        req.system_prompt = current_prompt

    # ==================== Commands ====================

    @filter.command("忙碌日程", alias={"busy show", "busy schedule"})
    async def cmd_show_schedule(self, event: AstrMessageEvent):
        """查看今日的日程和忙碌时段"""
        owner_date = self._get_effective_date()
        active = self._get_active_schedule()
        data = active.data if active else None

        if not data or active.source_owner_date != owner_date:
            yield event.plain_result("当前周期日程尚未生成，正在生成...")
            try:
                data = await self.generator.generate_schedule_or_wait(
                    owner_date, umo=event.unified_msg_origin
                )
            except Exception as e:
                if not data:
                    yield event.plain_result(f"日程生成失败：{e}")
                    return
                yield event.plain_result(
                    f"当前周期生成失败，继续显示上一份可用日程：{e}"
                )
        display_date = date.fromisoformat(data.date)

        # Build response
        response_parts = [
            f"📅 {display_date.strftime('%Y-%m-%d')}",
            "",
            f"👗 今日穿搭：{data.outfit}" + (f"\n发型：{data.hairstyle}" if data.hairstyle else ""),
            "",
            "📝 日程安排：",
            data.schedule,
            "",
        ]

        # Show current status
        if self.busy_mgr.is_busy:
            response_parts.extend(["", "💤 当前状态：忙碌中"])
        else:
            response_parts.extend(["", "✅ 当前状态：在线"])

        yield event.plain_result("\n".join(response_parts))

    @filter.command("忙碌重写", alias={"busy renew"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_renew_schedule(self, event: AstrMessageEvent, extra: str = ""):
        """重写今日日程（可附加补充要求）"""
        today = self._get_effective_date()

        if extra:
            yield event.plain_result(f"正在根据补充要求重写今日日程：{extra}")
        else:
            yield event.plain_result("正在重写今日日程...")

        try:
            data = await self.generator.generate_schedule(today, umo=event.unified_msg_origin, extra=extra if extra else None)
            hairstyle_line = f"\n发型：{data.hairstyle}" if data.hairstyle else ""
            yield event.plain_result(
                f"📅 {today.strftime('%Y-%m-%d')}\n"
                f"👗 今日穿搭：{data.outfit}{hairstyle_line}\n"
                f"📝 日程安排：\n{data.schedule}"
            )

            self._sync_schedule_to_context()

            # Refresh busy state so current_busy_period matches new schedule
            if self.busy_mgr.is_busy:
                now = datetime.now()
                current = self.busy_mgr.get_current_busy_period(now)
                if current:
                    self.busy_mgr._current_busy_period = current.period
                    self.busy_mgr._current_resolved_period = current
                    self.busy_mgr._current_busy_owner_date = current.owner_date
                    self.busy_mgr._current_busy_schedule_time = (
                        self.busy_mgr._parse_schedule_time()
                    )
                    logger.info(
                        f"[BusySchedule] Refreshed busy period after rewrite: "
                        f"{current.period.activity}"
                    )
                else:
                    # Current time is no longer in any busy period
                    await self.busy_mgr._exit_busy()
                    logger.info("[BusySchedule] Exited busy state after rewrite (no matching period)")

        except Exception as e:
            yield event.plain_result(f"日程重写失败：{e}")

    @filter.command("忙碌状态", alias={"busy status"})
    async def cmd_busy_status(self, event: AstrMessageEvent):
        """查看当前忙碌状态"""
        now = datetime.now()

        response_parts = ["📊 忙碌状态信息", ""]

        # Current status
        if self.busy_mgr.is_busy:
            activity = self.busy_mgr.current_activity or "未知活动"
            response_parts.append(f"💤 当前状态：忙碌中（{activity}）")
            resolved = self.busy_mgr._current_resolved_period
            if resolved:
                remaining_secs = (resolved.end - now).total_seconds()
                if remaining_secs > 0:
                    remaining_mins = int(remaining_secs / 60)
                    response_parts.append(f"⏱️ 剩余时间：约 {remaining_mins} 分钟")
        else:
            response_parts.append("✅ 当前状态：在线")

        # Next busy period
        next_resolved = self.busy_mgr.get_next_busy_period(now)
        if next_resolved:
            period = next_resolved.period
            response_parts.append(
                f"\n⏰ 下一个忙碌时段："
                f"{next_resolved.start.strftime('%m-%d %H:%M')}-"
                f"{next_resolved.end.strftime('%m-%d %H:%M')} {period.activity}"
            )

        # Chat protection status
        if self.busy_mgr._last_user_message_time:
            inactive_minutes = (now - self.busy_mgr._last_user_message_time).total_seconds() / 60
            protect_minutes = self._get_config("chat_protect_minutes", 10)
            if inactive_minutes < protect_minutes:
                remaining = protect_minutes - inactive_minutes
                response_parts.append(f"\n🛡️ 聊天保护中：还需 {int(remaining)} 分钟无消息才能进入忙碌")

        # Message queue stats
        queue_stats = self.interceptor.get_queue_stats()
        if queue_stats:
            response_parts.append("\n📨 待处理消息：")
            for user_id, stats in queue_stats.items():
                response_parts.append(f"  用户 {user_id[:8]}...：{stats['count']} 条消息")

        yield event.plain_result("\n".join(response_parts))

    @filter.command("忙碌帮助", alias={"busy help"})
    async def cmd_busy_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        help_text = """
🤖 AI忙碌时段管理 - 帮助

📋 命令列表：
• 忙碌日程 / busy show - 查看今日日程和忙碌时段
• 忙碌重写 / busy renew - 重写今日日程（管理员）
• 忙碌状态 / busy status - 查看当前忙碌状态
• 忙碌预览 / busy preview - 查看当前注入的提示词内容
• 设置忙碌 / busy set - 手动进入忙碌状态
• 解除忙碌 / busy clear - 手动解除忙碌状态
• 忙碌时长 / busy duration - 设置忙碌时长后自动解除
• 忙碌帮助 / busy help - 显示此帮助

💡 功能说明：
• AI会根据日程安排自动进入忙碌状态
• 忙碌时消息会被拦截并合并，忙完后统一处理
• 使用特殊关键词可以立即唤醒AI

🔑 唤醒关键词：
"""
        keywords = self._get_config("wake_keywords", ["咋不回我", "快点回我呀"])
        help_text += "、".join(keywords)

        yield event.plain_result(help_text.strip())

    # ==================== Test Commands ====================

    @filter.command("设置忙碌", alias={"busy set"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_set_busy(self, event: AstrMessageEvent, extra: str = ""):
        """设置当前为忙碌状态（测试用）"""
        activity = extra if extra else "测试忙碌"
        
        from .core.data import BusyPeriod
        now = datetime.now()
        period = BusyPeriod(
            start_time=now.strftime("%H:%M"),
            end_time=(now + timedelta(hours=1)).strftime("%H:%M"),
            activity=activity,
            is_busy=True,
        )
        
        await self.busy_mgr._enter_busy(period)
        yield event.plain_result(f"已设置为忙碌状态：{activity}")

    @filter.command("解除忙碌", alias={"busy clear"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_clear_busy(self, event: AstrMessageEvent):
        """解除当前忙碌状态（测试用）"""
        if self.busy_mgr.is_busy:
            await self.busy_mgr._exit_busy()
            yield event.plain_result("已解除忙碌状态")
        else:
            yield event.plain_result("当前已经是在线状态")

    @filter.command("忙碌时长", alias={"busy duration"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_busy_duration(self, event: AstrMessageEvent, extra: str = ""):
        """忙碌指定时长（分钟）。用法：忙碌时长 30"""
        if not extra:
            yield event.plain_result("请指定忙碌时长（分钟），例如：忙碌时长 30")
            return
        
        try:
            minutes = int(extra)
            if minutes <= 0:
                raise ValueError
        except ValueError:
            yield event.plain_result("时长必须是正整数（分钟）")
            return
        
        from .core.data import BusyPeriod
        now = datetime.now()
        period = BusyPeriod(
            start_time=now.strftime("%H:%M"),
            end_time=(now + timedelta(minutes=minutes)).strftime("%H:%M"),
            activity=f"忙碌{minutes}分钟",
            is_busy=True,
        )
        
        await self.busy_mgr._enter_busy(period)
        
        # Set timer to auto exit
        async def auto_exit():
            await asyncio.sleep(minutes * 60)
            if self.busy_mgr.is_busy:
                await self.busy_mgr._exit_busy()
        
        asyncio.create_task(auto_exit())
        yield event.plain_result(f"已进入忙碌状态，将在{minutes}分钟后自动解除")

    @filter.command("忙碌预览", alias={"busy preview", "忙碌注入"})
    async def cmd_preview_injection(self, event: AstrMessageEvent):
        """展示当前注入到 LLM 的提示词内容"""
        now = datetime.now()
        active = self._get_active_schedule(now)
        if not active:
            yield event.plain_result("当前没有可用的已完成日程")
            return
        data = active.data
        timeline = self._get_resolved_timeline(active.owner_date)

        # Part 0: custom user-defined injection
        custom_text = self.injector.build_custom_injection()

        # Part 1: static (outfit + schedule)
        static_text = self.injector.build_static_injection(data)

        # Part 2: semi-static (current + next activity)
        schedule_text = self.injector.build_schedule_injection(
            data, timeline, now
        )

        # Part 3: busy flag (only when busy)
        busy_text = ""
        if self.busy_mgr.is_busy and self.busy_mgr.current_activity:
            period = BusyPeriod(start_time="", end_time="", activity=self.busy_mgr.current_activity)
            busy_text = self.injector.build_busy_state_injection(period)

        # Build preview
        parts = [
            "=" * 30,
            "📋 提示词注入预览",
            "=" * 30,
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "【缓存块 · 穿搭+日程+活动状态】",
            "📍 注入位置：system_prompt 中，人设之后",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━",
            static_text if static_text else "（无内容 - 日程未生成）",
        ]

        if custom_text:
            parts.extend([
                "",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "【自定义注入词】",
                "📍 注入位置：日程安排之后，当前活动之前",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━",
                custom_text,
            ])

        parts.extend([
            "",
            schedule_text if schedule_text else "（无活动状态）",
        ])

        if busy_text:
            parts.extend([
                "",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "【动态块 · 忙碌标记】",
                "📍 注入位置：system_prompt 末尾",
                "🔄 更新频率：进入/退出忙碌时",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━",
                busy_text,
            ])

        parts.extend(["", "=" * 30])

        yield event.plain_result("\n".join(parts))