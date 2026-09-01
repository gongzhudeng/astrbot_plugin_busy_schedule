"""AI Busy Schedule Plugin - Let AI have a real life rhythm."""

import asyncio
import json
import random
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.message.components import Image, Plain
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_tools import StarTools

from .core.busy_manager import BusyPeriodManager
from .core.calendar_context import build_calendar_context
from .core.chat_protection import (
    is_natural_spark_proactive,
    is_usable_assistant_response,
)
from .core.data import (
    ActiveSchedule,
    BusyPeriod,
    MediaExecutionRecord,
    MediaExecutionStore,
    ResolvedPeriod,
    ScheduleDataManager,
    get_schedule_owner_date,
    parse_clock_time,
    parse_schedule_time,
    resolve_schedule_periods,
)
from .core.generator import (
    _SCHEMA_DEFAULTS,
    DeterministicScheduleError,
    ScheduleGenerator,
)
from .core.image_renderer import BusyScheduleImageRenderer, BusyStatusImageData
from .core.message_input import is_slash_prefixed_message
from .core.message_interceptor import MessageInterceptor
from .core.prompt_injector import PromptInjector
from .core.request_content import replace_temp_user_content
from .core.schedule_editor import (
    ScheduleEditError,
    ScheduleEditor,
)
from .core.weather import WeatherService


def _is_slash_prefixed_input(event: AstrMessageEvent) -> bool:
    """Return whether the first non-empty source text starts with a slash."""
    marker = "_slash_prefixed_input"
    cached = event.get_extra(marker, None)
    if isinstance(cached, bool):
        return cached

    is_slash = is_slash_prefixed_message(event.get_messages())
    event.set_extra(marker, is_slash)
    return is_slash


def _replace_prompt_block(
    prompt: str,
    marker: str,
    end_marker: str,
    content: str,
) -> str:
    """Replace, append, or remove one independently managed prompt block."""
    if not content:
        pattern = f"\\n*{re.escape(marker)}.*?{re.escape(end_marker)}\\n*"
        return re.sub(pattern, "", prompt, flags=re.DOTALL)

    block = f"{marker}\n{content}\n{end_marker}"
    if marker not in prompt:
        return f"{prompt}\n\n{block}"

    pattern = f"{re.escape(marker)}.*?{re.escape(end_marker)}"
    return re.sub(pattern, block, prompt, flags=re.DOTALL)


def _rebuild_system_prompt(prompt: str, blocks: dict[str, str]) -> str:
    """Rebuild this plugin's blocks in one deterministic order.

    The dynamic-persona hook has already finished when this runs, while the
    emotion-state hook runs later.  Removing and re-adding all owned markers on
    every request prevents stale blocks from surviving activity/state changes.
    """
    prompt = prompt or ""
    markers = {
        "daily": ("<!-- BUSY_SCHEDULE_CACHE -->", "<!-- /BUSY_SCHEDULE_CACHE -->"),
        "custom": ("<!-- BUSY_SCHEDULE_CUSTOM -->", "<!-- /BUSY_SCHEDULE_CUSTOM -->"),
        "activity": (
            "<!-- BUSY_SCHEDULE_ACTIVITY -->",
            "<!-- /BUSY_SCHEDULE_ACTIVITY -->",
        ),
        "busy": ("<!-- BUSY_SCHEDULE_BUSY -->", "<!-- /BUSY_SCHEDULE_BUSY -->"),
        "execution": (
            "<!-- BUSY_SCHEDULE_EXECUTION -->",
            "<!-- /BUSY_SCHEDULE_EXECUTION -->",
        ),
    }
    legacy_calendar_marker = (
        "<!-- BUSY_SCHEDULE_CALENDAR -->",
        "<!-- /BUSY_SCHEDULE_CALENDAR -->",
    )
    anchor = "<!-- EMOTION_STATE_ANCHOR -->"
    emotion_start = "<!-- EMOTION_STATE_BEGIN -->"
    emotion_end = "<!-- /EMOTION_STATE_END -->"
    emotion_pattern = f"{re.escape(emotion_start)}.*?{re.escape(emotion_end)}"
    match = re.search(emotion_pattern, prompt, flags=re.DOTALL)
    emotion_block = match.group(0) if match else ""
    cleaned = re.sub(
        f"(?:\r?\n)*{emotion_pattern}(?:\r?\n)*", "\n\n", prompt, flags=re.DOTALL
    )
    cleaned = re.sub(f"(?:\r?\n)*{re.escape(anchor)}(?:\r?\n)*", "\n\n", cleaned)
    for marker, end_marker in (legacy_calendar_marker, *markers.values()):
        cleaned = re.sub(
            f"(?:\r?\n)*{re.escape(marker)}.*?{re.escape(end_marker)}(?:\r?\n)*",
            "\n\n",
            cleaned,
            flags=re.DOTALL,
        )

    ordered = [
        (legacy_calendar_marker, blocks.get("calendar", "")),
        (markers["daily"], blocks.get("daily", "")),
        (markers["custom"], blocks.get("custom", "")),
        (markers["activity"], blocks.get("activity", "")),
        (markers["busy"], blocks.get("busy", "")),
    ]
    sections = []
    for (marker, end_marker), content in ordered:
        if content and content.strip():
            sections.append(f"{marker}\n{content.strip()}\n{end_marker}")
    sections.append(anchor)
    if emotion_block:
        sections.append(emotion_block)
    execution = blocks.get("execution", "")
    if execution and execution.strip():
        marker, end_marker = markers["execution"]
        sections.append(f"{marker}\n{execution.strip()}\n{end_marker}")
    suffix = "\n\n".join(sections)
    return f"{cleaned.rstrip()}\n\n{suffix}" if cleaned.strip() else suffix


@register(
    "astrbot_plugin_busy_schedule",
    "灵犀 · AI忙碌时段管理",
    "让AI拥有真实的生活节奏！自动计算忙碌时段、智能拦截合并消息、特殊关键词唤醒",
    "v2.12.3",
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
        self.data_mgr: ScheduleDataManager | None = None
        self.generator: ScheduleGenerator | None = None
        self.busy_mgr: BusyPeriodManager | None = None
        self.interceptor: MessageInterceptor | None = None
        self.injector: PromptInjector | None = None
        self.schedule_editor: ScheduleEditor | None = None
        self.weather_service: WeatherService | None = None
        self.image_renderer = BusyScheduleImageRenderer(Path(__file__).parent)
        self._schedule_edit_lock = asyncio.Lock()

        # Background tasks
        self._state_check_task: asyncio.Task | None = None
        self._schedule_gen_task: asyncio.Task | None = None
        self._daily_refresh_task: asyncio.Task | None = None

        self._last_refresh_owner_date: date | None = None
        self._refresh_retry_owner_date: date | None = None
        self._refresh_retry_after: datetime | None = None

        # Peek state: probability stays latched until its delivery transaction finishes
        self._peek_timers: dict[str, asyncio.Task] = {}
        self._peek_latched: set[str] = set()

        # One queued-message delivery transaction per user
        self._delivery_tasks: dict[str, asyncio.Task] = {}
        self._delivery_locks: dict[str, asyncio.Lock] = {}
        self._suppress_exit_delivery = False

        # Periodic poll task: background loop that fires while busy
        self._busy_poll_task: asyncio.Task | None = None

        # Target umo for daily schedule generation (persisted across restarts)
        self._schedule_target_umo: str | None = None
        self._state_file: Path | None = None
        self.media_execution = MediaExecutionStore()
        self._media_operation_counter = 0

    async def initialize(self):
        """Initialize plugin and all modules."""
        logger.info("[BusySchedule] Initializing plugin...")

        # Initialize modules
        self._state_file = self.data_dir / "plugin_state.json"
        self._load_state()
        self.data_mgr = ScheduleDataManager(self.schedule_data_file)
        self.weather_service = WeatherService(
            self._get_config, self.data_dir / "weather_cache.json"
        )
        self.generator = ScheduleGenerator(
            self.context,
            self.config,
            self.data_mgr,
            weather_service=self.weather_service,
        )
        self.busy_mgr = BusyPeriodManager(self.config, self.data_mgr)
        self.interceptor = MessageInterceptor(
            self.config,
            timezone=self._get_astrbot_timezone(),
        )
        self.injector = PromptInjector(self.config)
        self.schedule_editor = ScheduleEditor()

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
        self.context._busy_schedule_get_facts = self._export_facts
        self.context._busy_schedule_record_media_success = self.record_media_success
        logger.info(
            "[BusySchedule] Structured timeline interface registered "
            f"(owner_date={self._get_effective_date().isoformat()})"
        )

        # Start background tasks
        self._state_check_task = asyncio.create_task(self._state_check_loop())

        # Schedule generation as background task to avoid blocking initialization
        self._schedule_gen_task = asyncio.create_task(
            self._ensure_today_schedule_async()
        )

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

        exported_callbacks = {
            "_busy_schedule_get_timeline": self._export_timeline,
            "_busy_schedule_get_facts": self._export_facts,
            "_busy_schedule_record_media_success": self.record_media_success,
        }
        for name, callback in exported_callbacks.items():
            if getattr(self.context, name, None) == callback:
                delattr(self.context, name)

        logger.info("[BusySchedule] Plugin terminated")

    def _disable_cycle_retries(self, owner_date: date, error: Exception) -> None:
        """Stop automatic retries for a failed schedule cycle."""
        self._last_refresh_owner_date = owner_date
        self._refresh_retry_owner_date = None
        self._refresh_retry_after = None
        logger.error(
            f"[BusySchedule] Schedule protocol failed for {owner_date}; "
            f"automatic retries disabled for this cycle; run a manual generation "
            f"command to retry: {error}"
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
                self._disable_cycle_retries(owner_date, e)

        self._sync_schedule_to_context()

    def _get_active_schedule(
        self, now: datetime | None = None
    ) -> ActiveSchedule | None:
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
        schedule_time = parse_schedule_time(self._get_config("schedule_time", "07:00"))
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
                periods = resolve_schedule_periods(active, schedule_time, next_active)
            except ValueError as exc:
                logger.warning(
                    f"[BusySchedule] Failed to resolve cycle {cycle_date}: {exc}"
                )
                continue
            if cycle_date == owner_date - timedelta(days=1):
                periods = [item for item in periods if item.period.is_open_sleep]
            resolved.extend(periods)
        return sorted(resolved, key=lambda item: item.start)

    def _export_timeline(self, owner_date: date | None = None) -> list[dict]:
        """Return a framework-neutral schedule timeline for downstream plugins."""
        target_date = owner_date or self._get_effective_date()
        active = self.data_mgr.get_active(target_date)
        if not active:
            return []

        resolved_by_period = {
            id(item.period): item for item in self._get_resolved_timeline(target_date)
        }
        schedule_time = parse_schedule_time(self._get_config("schedule_time", "07:00"))
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
                    "is_busy": period.is_busy,
                    "start": start,
                    "end": end,
                    "valid": valid,
                    "error": error,
                }
            )
        return timeline

    def _export_facts(self, now: datetime | None = None) -> dict:
        """Expose schedule and activity facts without deriving emotional state."""
        current_time = now or datetime.now()
        active = self._get_active_schedule(current_time)
        if not active:
            return {
                "owner_date": "",
                "current_activity": "",
                "next_activity": "",
                "outfit": "",
                "hairstyle": "",
                "is_busy": False,
                "is_sleeping": False,
                "weather": "",
                "timeline": [],
            }
        resolved = self._get_resolved_timeline(active.owner_date)
        current, next_period = self.injector._get_activity_state(resolved, current_time)
        weather = getattr(active.data, "weather", None)
        return {
            "owner_date": active.owner_date.isoformat(),
            "current_activity": current.period.activity if current else "",
            "next_activity": next_period.period.activity if next_period else "",
            "outfit": str(getattr(active.data, "outfit", "") or ""),
            "hairstyle": str(getattr(active.data, "hairstyle", "") or ""),
            "is_busy": bool(self.busy_mgr and self.busy_mgr.is_busy),
            "is_sleeping": bool(current and current.period.is_sleep),
            "weather": weather.format_summary() if weather else "",
            "timeline": self._export_timeline(active.owner_date),
        }

    @staticmethod
    def _media_activity_key(
        active: ActiveSchedule, current_period: ResolvedPeriod | None
    ) -> str:
        """Return a stable execution scope for the current activity or free time."""
        if current_period:
            return (
                f"{current_period.start.isoformat()}:{current_period.period.activity}"
            )
        return f"{active.owner_date.isoformat()}:free_time"

    def _sync_media_execution(
        self,
        active: ActiveSchedule,
        current_period: ResolvedPeriod | None,
    ) -> None:
        activity_key = self._media_activity_key(active, current_period)
        previous_key = self.media_execution.record.activity_key
        self.media_execution.sync(active.owner_date, activity_key)
        if previous_key != activity_key:
            self._save_state()
            logger.info(
                "[BusySchedule] Media execution scope changed: "
                f"{previous_key or '(empty)'} -> {activity_key}; "
                f"current_counts={self.media_execution.record.current_counts}"
            )

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
            current_period = next(
                (item for item in timeline if item.contains(now)), None
            )
            self._sync_media_execution(active, current_period)
            current = self.injector._find_current_activity(timeline, now)
            self.context._busy_schedule_current_activity = (
                f"{current}（正在进行）" if current else ""
            )
            candidates = [item for item in timeline if item.start > now]
            if candidates:
                resolved = min(candidates, key=lambda item: item.start)
                self.context._busy_schedule_next_activity = f"{resolved.period.activity}（尚未开始，{resolved.start.strftime('%H:%M')}开始）"
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
                    logger.info(f"[BusySchedule] Schedule cycle {owner_date} refreshed")
                except DeterministicScheduleError as e:
                    self._disable_cycle_retries(owner_date, e)
                except Exception as e:
                    self._disable_cycle_retries(owner_date, e)

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

    def _current_period(self) -> BusyPeriod | None:
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
                self._suppress_exit_delivery = True
                try:
                    await self.busy_mgr.wake_up(reason)
                finally:
                    self._suppress_exit_delivery = False

            return await self._send_merged_messages(user_id, period, reason)

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
            keyword, prob_str, interval_str = (
                parts[0].strip(),
                parts[1].strip(),
                parts[2].strip(),
            )
            if keyword and keyword in activity:
                try:
                    prob = self._normalized_probability(prob_str)
                    mn, mx = interval_str.split("-")
                    mn_value, mx_value = self._normalized_range(mn, mx)
                    return prob, mn_value, mx_value
                except Exception:
                    continue
        # Global defaults
        prob = self._normalized_probability(self._get_config("poll_probability", 0.3))
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
                    if (
                        user_id in self._peek_timers
                        and not self._peek_timers[user_id].done()
                    ):
                        continue
                    # Quiet period check: skip if user sent a message recently
                    if quiet > 0:
                        queue_msgs = self.interceptor.get_queued_messages(user_id)
                        if queue_msgs:
                            newest = max(
                                self.interceptor._parse_timestamp(m["timestamp"])
                                for m in queue_msgs
                            )
                            if (
                                self.interceptor._now() - newest
                            ).total_seconds() < quiet:
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
        self, user_id: str, period: BusyPeriod, reason: str = "unknown"
    ) -> bool:
        """Re-inject one merged queue and commit it only after enqueue succeeds."""
        user_message = self.interceptor.get_merged_user_message(user_id)
        if not user_message:
            return False

        delivery_payload = self.interceptor.build_delivery_payload(
            user_id,
            period.start_time,
            period.end_time or datetime.now().strftime("%H:%M"),
            reason=reason,
            activity=period.activity,
        )
        if not delivery_payload:
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

        # Build a clean event with the newest queued message as the real user input.
        # The merged explanation is attached later as provider-only user content.
        last_message = user_message
        wake_prefixes = self.context.get_config().get("wake_prefix", ["/"])
        wake_prefix = wake_prefixes[0] if wake_prefixes else "/"
        prefixed_text = wake_prefix + last_message

        reinjected_message = last_event.message_obj.__class__()
        reinjected_message.__dict__.update(last_event.message_obj.__dict__)
        reinjected_message.type = last_event.get_message_type()
        reinjected_message.message_str = prefixed_text
        reinjected_message.raw_message = getattr(
            last_event.message_obj, "raw_message", None
        )
        reinjected_message.self_id = last_event.get_self_id()
        reinjected_message.sender = last_event.message_obj.sender
        reinjected_message.group = getattr(last_event.message_obj, "group", None)
        reinjected_message.session_id = last_event.session_id
        reinjected_message.message_id = getattr(
            last_event.message_obj, "message_id", None
        )
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
            event_kwargs["interaction_followup_webhook"] = (
                last_event.interaction_followup_webhook
            )

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

        reinjected_event.set_extra("busy_schedule_merged", True)
        # The request hook renders this metadata at the actual processing time.
        # Message bodies remain solely in the real user message and are not copied
        # into the temporary wake explanation or conversation history.
        reinjected_event.set_extra("busy_schedule_wake_event", delivery_payload)
        # Alias retained for integrations that call this a delivery event.
        reinjected_event.set_extra("busy_schedule_delivery_event", delivery_payload)
        # Also mark as chat_merger merged so chat_merger does not re-intercept
        reinjected_event.set_extra("chat_merger_merged", True)

        logger.info(
            f"[BusySchedule] Sending merged messages for {user_id}: "
            f"{len(user_message)} chars, {delivery_payload['message_count']} messages"
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
        match_mode = self._get_config(
            "busy_filter_keyword_match_mode", "包含关键词模式"
        )
        for keyword in keywords:
            if match_mode == "完全匹配模式":
                if message_text.strip() == keyword:
                    return True
            else:  # 包含关键词模式
                if keyword in message_text:
                    return True
        return False

    def record_media_success(
        self,
        umo: str,
        media_types: set[str],
        operation_id: str | None = None,
    ) -> bool:
        """Commit one successful media operation for the current activity."""
        now = datetime.now()
        valid_types = media_types & {"image", "voice"}
        if not valid_types:
            logger.warning(
                "[BusySchedule] Media record ignored: no valid media types "
                f"(umo={umo}, media_types={media_types})"
            )
            return False

        active = self._get_active_schedule(now)
        if not active:
            logger.warning(
                "[BusySchedule] Media record ignored: no active completed schedule "
                f"(umo={umo}, now={now.isoformat()})"
            )
            return False
        timeline = self._get_resolved_timeline(active.owner_date)
        current = next((item for item in timeline if item.contains(now)), None)
        operation_id = operation_id or f"{umo}:{now.timestamp()}"
        activity_key = self._media_activity_key(active, current)
        committed = self.media_execution.record_success(
            active.owner_date, activity_key, operation_id, valid_types
        )
        if committed:
            self._save_state()
            logger.info(
                "[BusySchedule] Media record committed: "
                f"owner_date={active.owner_date.isoformat()}, "
                f"activity={activity_key}, types={sorted(valid_types)}, "
                f"current_counts={self.media_execution.record.current_counts}, "
                f"cycle_counts={self.media_execution.record.cycle_counts}"
            )
        else:
            logger.debug(
                "[BusySchedule] Media record ignored as duplicate: "
                f"operation_id={operation_id}"
            )
        return committed

    def _load_state(self):
        """Load persisted schedule target and media execution state."""
        if not self._state_file or not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            self._schedule_target_umo = data.get("schedule_target_umo") or None
            media_record = data.get("media_execution")
            if isinstance(media_record, dict):
                self.media_execution = MediaExecutionStore(
                    MediaExecutionRecord.from_dict(media_record)
                )
            if self._schedule_target_umo:
                logger.info(
                    f"[BusySchedule] Loaded schedule_target_umo: {self._schedule_target_umo}"
                )
        except Exception as e:
            logger.warning(f"[BusySchedule] Failed to load plugin state: {e}")

    def _save_state(self):
        """Persist schedule target and media execution state."""
        if not self._state_file:
            return
        try:
            data = {
                "schedule_target_umo": self._schedule_target_umo or "",
                "media_execution": self.media_execution.to_dict(),
            }
            self._state_file.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[BusySchedule] Failed to save plugin state: {e}")

    def _get_config(self, key: str, default=None):
        """Get config value with schema default fallback."""
        # Nested groups take priority — user-edited values live here
        for group_name in [
            "基础设置",
            "图片显示",
            "忙碌时段",
            "随机接收",
            "定时检查",
            "关键词设置",
            "消息合并",
            "日程生成",
            "天气服务",
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

    def _get_astrbot_timezone(self) -> str | None:
        """Read AstrBot's global timezone for delayed-message timestamps."""
        try:
            config = self.context.get_config()
            if hasattr(config, "get"):
                value = config.get("timezone")
                return str(value) if value else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("[BusySchedule] Cannot read AstrBot timezone: %s", exc)
        return None

    def _now_in_astrbot_timezone(self) -> datetime:
        """Return current time using AstrBot's configured timezone when valid."""
        value = self._get_astrbot_timezone()
        if value:
            try:
                return datetime.now(ZoneInfo(value))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[BusySchedule] Invalid AstrBot timezone %r; using local timezone: %s",
                    value,
                    exc,
                )
        return datetime.now().astimezone()

    def _configure_schedule_edit_tool(self, req: ProviderRequest) -> bool:
        """Apply the per-request schedule editor switch to the available tool set."""
        enabled = bool(self._get_config("enabled", True)) and bool(
            self._get_config("schedule_edit_enabled", True)
        )
        if not enabled and req.func_tool is not None:
            req.func_tool.remove_tool("edit_current_schedule")
        return enabled

    # ==================== Event Handlers ====================

    @filter.event_message_type(EventMessageType.ALL, priority=10)
    async def on_message(self, event: AstrMessageEvent):
        """处理收到的消息，并根据当前忙碌状态决定放行、排队或唤醒。"""
        if not self._get_config("enabled", True):
            return

        # Media-producing plugins use this callback after their platform send succeeds.
        event._busy_schedule_record_media_success = self.record_media_success

        # Skip events already processed by busy_schedule merge
        if event.get_extra("busy_schedule_merged", False):
            return

        message_text = event.message_str.strip()
        user_id = event.unified_msg_origin

        # Skip empty messages
        if not message_text:
            return

        # Slash commands are not real chat - skip interception and chat protection
        if _is_slash_prefixed_input(event):
            return

        # Check for wake keywords first
        if self._check_wake_keywords(message_text):
            extra_comps = (
                [c for c in event.message_obj.message if not isinstance(c, Plain)]
                if hasattr(event.message_obj, "message")
                else []
            )
            has_queue = self.interceptor.has_queued_messages(user_id)
            if self.busy_mgr.is_busy:
                self._cancel_peek_timer(user_id, clear_latch=True)
                self.interceptor.queue_message(
                    user_id,
                    message_text,
                    event,
                    extra_components=extra_comps,
                )
                await self.busy_mgr.wake_up("keyword")
                event.stop_event()
                return
            elif has_queue:
                # state_check_loop may have already exited busy before the wake keyword arrived;
                # the queue is still non-empty so flush it now.
                now = datetime.now()
                period = self.busy_mgr._current_busy_period or BusyPeriod(
                    start_time="??:??",
                    end_time=now.strftime("%H:%M"),
                    activity="忙碌时段",
                )
                await self._send_merged_messages(user_id, period, reason="keyword")
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

            extra_comps = (
                [c for c in event.message_obj.message if not isinstance(c, Plain)]
                if hasattr(event.message_obj, "message")
                else []
            )

            result = self.interceptor.queue_message(
                user_id,
                message_text,
                event,
                extra_components=extra_comps,
            )

            if result == "queued":
                event.stop_event()
                if not self._is_sleeping() and self._get_config("peek_enabled", False):
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

    @filter.on_agent_begin()
    async def on_agent_begin(self, event: AstrMessageEvent, *_args):
        """Guard busy entry while a normal conversational Agent is running."""
        if not self._get_config("enabled", True):
            return
        if event.get_extra("spark_proactive_retrieval", False):
            return
        if isinstance(event, CronMessageEvent):
            return
        if _is_slash_prefixed_input(event):
            return
        token = id(event)
        event.set_extra("busy_schedule_chat_agent", token)
        self.busy_mgr.mark_reply_inflight(token)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response):
        """Refresh protection after a normal Agent returns usable assistant text."""
        token = event.get_extra("busy_schedule_chat_agent", None)
        if not isinstance(token, int):
            return
        if is_usable_assistant_response(response):
            self.busy_mgr.record_chat_model_activity()
            logger.debug(
                "[BusySchedule] Chat protection refreshed by main Agent response"
            )

    @filter.on_agent_done()
    async def on_agent_done(self, event: AstrMessageEvent, *_args):
        """Always release the in-flight conversational reply guard."""
        token = event.get_extra("busy_schedule_chat_agent", None)
        if isinstance(token, int):
            self.busy_mgr.clear_reply_inflight(token)
            event.set_extra("busy_schedule_chat_agent", None)

    @filter.on_llm_request(priority=-100)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """Inject ordered system state and render delayed-message timing context."""
        replace_temp_user_content(req)
        self._configure_schedule_edit_tool(req)
        if not self._get_config("enabled", True):
            return

        if is_natural_spark_proactive(event):
            self.busy_mgr.record_chat_model_activity()
            logger.debug(
                "[BusySchedule] Chat protection refreshed by Spark proactive request"
            )

        # Record the umo for use in daily auto-generation
        umo = event.unified_msg_origin
        if umo and umo != self._schedule_target_umo:
            self._schedule_target_umo = umo
            self._save_state()

        now = datetime.now()
        calendar_date = self._now_in_astrbot_timezone().date()
        active = self._get_active_schedule(now)
        data = active.data if active else None
        timeline = self._get_resolved_timeline(active.owner_date) if active else []
        current_period = (
            next((item for item in timeline if item.contains(now)), None)
            if timeline
            else None
        )
        if active:
            self._sync_media_execution(active, current_period)

        build_static = getattr(self.injector, "build_static_injection", None)
        if callable(build_static):
            static_injection = build_static(data, calendar_date=calendar_date)
        else:  # compatibility with injectors supplied by older integrations/tests
            build_calendar = getattr(self.injector, "build_calendar_injection", None)
            static_injection = ""
            if callable(build_calendar):
                legacy_calendar = build_calendar(calendar_date)
            else:
                legacy_calendar = ""
        if not callable(build_static):
            calendar_injection = legacy_calendar
        else:
            calendar_injection = ""
        activity_injection = (
            self.injector.build_activity_injection(data, timeline, now) if data else ""
        )
        execution_injection = (
            self.injector.build_execution_injection(self.media_execution.to_dict())
            if active
            else ""
        )
        custom_injection = self.injector.build_custom_injection()

        busy_injection = ""
        if self.busy_mgr.is_busy:
            period = BusyPeriod(
                start_time="",
                end_time="",
                activity=self.busy_mgr.current_activity or "",
            )
            busy_injection = self.injector.build_busy_state_injection(period)

        req.system_prompt = _rebuild_system_prompt(
            req.system_prompt or "",
            {
                "custom": custom_injection,
                "daily": static_injection,
                "calendar": calendar_injection,
                "activity": activity_injection,
                "busy": busy_injection,
                "execution": execution_injection,
            },
        )

        wake_event = event.get_extra("busy_schedule_wake_event", None)
        if not isinstance(wake_event, dict):
            wake_event = event.get_extra("busy_schedule_delivery_event", None)
        if isinstance(wake_event, dict):
            wake_content = self.interceptor.render_wake_context(
                wake_event,
                processing_time=self._now_in_astrbot_timezone(),
            )
        else:
            # Compatibility with events produced by an older in-flight plugin
            # task; new deliveries always use the structured event above.
            wake_content = event.get_extra("busy_schedule_temp_user_content", "")
        replace_temp_user_content(req, wake_content)

    async def _refresh_after_schedule_edit(self) -> None:
        """Synchronize prompt context and busy state after an atomic edit."""
        self._sync_schedule_to_context()
        await self.busy_mgr.check_and_update_state()

    @filter.llm_tool(name="edit_current_schedule")
    async def edit_current_schedule(
        self,
        event: AstrMessageEvent,
        operations_json: str,
        mode: str = "commit",
        reason: str = "",
        confirmed_important: bool = False,
    ) -> str:
        """当用户明确提出需要修改日程或修改穿搭时，使用此工具编辑当前日程周期中尚未结束的安排。过去的活动不可修改。

        Args:
            operations_json(string): 原子操作 JSON 数组。过去活动不可修改；当前普通活动只能改 end_time；未来普通活动可新增、更新或删除。add 使用 start_time、end_time、activity、is_busy；update/remove 用 target_start_time 和可选 target_activity 定位，update 可设置 start_time、end_time、activity、is_busy。修改日程时，应注意查看今天的降雨时段；如果新增或修改后的活动落在降雨时段内，需要在调整结果中说明天气影响，并给出必要提醒，例如携带或使用雨伞、为户外活动预留避雨时间，或在合理情况下调整活动安排。调整今日整体或局部穿搭一律用 set_outfit，传 outfit 和可选 outfit_style、hairstyle；不传 hairstyle 表示保留，用户要求去掉发型时显式传空字符串。穿搭变化后检查后续活动是否引用旧穿搭，在同一数组中同步更新，并通常新增未来换衣活动。
            mode(string): commit 验证后立即保存；check 只验证不保存。删除重要活动可能需要确认时先用 check；不要向用户展示原始 JSON、机械预览或表格。
            reason(string): 本次调整的简短对话原因，应基于完整对话、自身意愿和当前日程。
            confirmed_important(boolean): 仅在用户已经确认删除未来重要活动后设为 true，否则保持 false。
        """
        if not self._get_config("enabled", True):
            return json.dumps(
                {"status": "unavailable", "message": "busy schedule is disabled"},
                ensure_ascii=False,
            )
        if not self._get_config("schedule_edit_enabled", True):
            return json.dumps(
                {"status": "unavailable", "message": "schedule editing is disabled"},
                ensure_ascii=False,
            )

        try:
            operations = json.loads(operations_json)
        except (TypeError, json.JSONDecodeError) as exc:
            return json.dumps(
                {"status": "invalid", "message": f"invalid operations JSON: {exc}"},
                ensure_ascii=False,
            )
        if not isinstance(operations, list) or not all(
            isinstance(item, dict) for item in operations
        ):
            return json.dumps(
                {
                    "status": "invalid",
                    "message": "operations_json must be a JSON array of objects",
                },
                ensure_ascii=False,
            )

        normalized_mode = mode.strip().lower()
        if normalized_mode not in {"check", "commit"}:
            return json.dumps(
                {"status": "invalid", "message": "mode must be check or commit"},
                ensure_ascii=False,
            )

        async with self._schedule_edit_lock:
            owner_date = self._get_effective_date()
            data = self.data_mgr.get(owner_date)
            if not data or data.status != "completed":
                return json.dumps(
                    {
                        "status": "unavailable",
                        "message": "the current cycle has no editable completed schedule",
                    },
                    ensure_ascii=False,
                )

            try:
                result = self.schedule_editor.apply(
                    data,
                    operations,
                    owner_date=owner_date,
                    schedule_time=parse_schedule_time(
                        self._get_config("schedule_time", "07:00")
                    ),
                    now=datetime.now(),
                    confirmed_important=confirmed_important,
                )
            except ScheduleEditError as exc:
                return json.dumps(
                    {
                        "status": exc.code,
                        "message": str(exc),
                        "reason": reason,
                    },
                    ensure_ascii=False,
                )

            if normalized_mode == "check":
                return json.dumps(
                    {
                        "status": "valid",
                        "message": "the adjustment is valid but was not saved",
                        "changes": result.changes,
                    },
                    ensure_ascii=False,
                )

            self.data_mgr.set(owner_date, result.data)
            await self._refresh_after_schedule_edit()
            logger.info(
                f"[BusySchedule] Current schedule edited: owner_date={owner_date}, "
                f"changes={len(result.changes)}, reason={reason or 'not provided'}"
            )
            return json.dumps(
                {
                    "status": "saved",
                    "message": "the current schedule was updated",
                    "changes": result.changes,
                    "outfit": result.data.outfit,
                    "hairstyle": result.data.hairstyle,
                    "last_updated": result.data.last_updated,
                },
                ensure_ascii=False,
            )

    # ==================== Commands ====================

    @filter.command("天气测试", alias={"天气查询", "busy weather"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_test_weather(self, event: AstrMessageEvent):
        """强制查询天气并展示日程生成 Prompt 中的天气文本。"""
        if not self.weather_service:
            yield event.plain_result("天气服务尚未初始化，请重新加载插件后再试。")
            return

        owner_date = self._get_effective_date()
        schedule_time = parse_schedule_time(self._get_config("schedule_time", "07:00"))
        providers = self._get_config("weather_providers", ["qweather", "open_meteo"])
        provider_text = (
            " → ".join(str(item) for item in providers)
            if isinstance(providers, list)
            else str(providers)
        )

        yield event.plain_result(
            "正在强制查询天气（跳过缓存）...\n"
            f"位置：{self._get_config('weather_city', '')} "
            f"{self._get_config('weather_admin', '')} "
            f"{self._get_config('weather_country_code', '')}\n"
            f"供应商顺序：{provider_text}"
        )

        snapshot, errors = await self.weather_service.query_forecast(
            owner_date,
            schedule_time,
            force_refresh=True,
        )
        if not snapshot:
            details = "\n".join(f"- {item}" for item in errors)
            yield event.plain_result(
                "天气查询失败，日程生成仍会按无天气模式继续。\n"
                f"周期：{owner_date.isoformat()} "
                f"{schedule_time[0]:02d}:{schedule_time[1]:02d} 起 24 小时\n"
                f"诊断：\n{details or '- 未返回具体错误'}"
            )
            return

        fallback_note = ""
        if errors:
            fallback_note = "\n前序供应商失败并已回退：\n" + "\n".join(
                f"- {item}" for item in errors
            )
        yield event.plain_result(
            "天气查询成功。\n"
            f"实际供应商：{snapshot.provider}\n"
            f"周期：{snapshot.cycle_start} → {snapshot.cycle_end}\n"
            f"缓存：已刷新 {self.weather_service.cache_file.name}"
            f"{fallback_note}\n\n"
            "以下内容就是日程模板中 {weather_forecast} 的实际值：\n\n"
            f"{snapshot.format_for_prompt()}"
        )

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
        calendar_context = build_calendar_context(display_date, self.config)
        is_current_cycle = display_date == owner_date
        weather_line = (
            f"🌦️ 天气：{data.weather.format_summary()}"
            if is_current_cycle and data.weather
            else "🌦️ 天气：暂不可用"
        )
        source_line = (
            ""
            if is_current_cycle
            else f"⚠️ 当前显示 {display_date.isoformat()} 的备用日程，天气不映射到当前周期"
        )

        # Build response
        response_parts = [
            f"📅 日期：{calendar_context['date_str']}",
            f"📆 星期：{calendar_context['weekday']}",
            f"🌙 农历：{calendar_context['lunar_date'] or '未知'}",
            f"🎋 节日：{calendar_context['holiday']}",
            f"⭐ 自定义特别日：{calendar_context['special_days'].replace(chr(10), '；')}",
            *([source_line] if source_line else []),
            weather_line,
            "",
            f"👗 今日穿搭：{data.outfit}"
            + (f"\n发型：{data.hairstyle}" if data.hairstyle else ""),
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

        try:
            png = self.image_renderer.render_schedule(
                data=data,
                now=self._now_in_astrbot_timezone(),
                is_busy=self.busy_mgr.is_busy,
                mode=self._get_config("schedule_image_theme", "自动切换"),
                source_note=source_line,
                calendar_context=calendar_context,
            )
            yield event.chain_result([Image.fromBytes(png)])
        except Exception as exc:  # noqa: BLE001
            logger.exception("[BusySchedule] Schedule image rendering failed: %s", exc)
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
            data = await self.generator.generate_schedule(
                today, umo=event.unified_msg_origin, extra=extra if extra else None
            )
            hairstyle_line = f"\n发型：{data.hairstyle}" if data.hairstyle else ""
            weather_line = data.weather.format_summary() if data.weather else "暂不可用"
            yield event.plain_result(
                f"📅 {today.strftime('%Y-%m-%d')}\n"
                f"🌦️ 天气：{weather_line}\n"
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
                    logger.info(
                        "[BusySchedule] Exited busy state after rewrite (no matching period)"
                    )

        except Exception as e:
            yield event.plain_result(f"日程重写失败：{e}")

    @filter.command("忙碌状态", alias={"busy status"})
    async def cmd_busy_status(self, event: AstrMessageEvent):
        """查看当前忙碌状态"""
        now = datetime.now()

        response_parts = ["📊 忙碌状态信息", ""]
        activity = ""
        remaining_mins = None
        current_start = ""
        current_end = ""
        current_timeline = next(
            (
                item
                for item in self._get_resolved_timeline(self._get_effective_date())
                if item.contains(now)
            ),
            None,
        )

        # Current status
        if self.busy_mgr.is_busy:
            activity = self.busy_mgr.current_activity or "未知活动"
            response_parts.append(f"💤 当前状态：忙碌中（{activity}）")
            resolved = self.busy_mgr._current_resolved_period
            if resolved:
                current_start = resolved.start.strftime("%H:%M")
                current_end = resolved.end.strftime("%H:%M")
                remaining_secs = (resolved.end - now).total_seconds()
                if remaining_secs > 0:
                    remaining_mins = int(remaining_secs / 60)
                    response_parts.append(f"⏱️ 剩余时间：约 {remaining_mins} 分钟")
        else:
            response_parts.append("✅ 当前状态：在线")
            if current_timeline:
                activity = current_timeline.period.activity
                current_start = current_timeline.start.strftime("%H:%M")
                current_end = current_timeline.end.strftime("%H:%M")
                response_parts.append(f"🎯 当前活动：{activity}")

        # Next busy period
        next_resolved = self.busy_mgr.get_next_busy_period(now)
        next_busy_text = ""
        if next_resolved:
            period = next_resolved.period
            next_busy_text = (
                f"{next_resolved.start.strftime('%m-%d %H:%M')}-"
                f"{next_resolved.end.strftime('%m-%d %H:%M')} {period.activity}"
            )
            response_parts.append(f"\n⏰ 下一个忙碌时段：{next_busy_text}")

        # Chat protection status
        chat_protection_text = ""
        if self.busy_mgr._last_chat_model_activity_time:
            inactive_minutes = (
                now - self.busy_mgr._last_chat_model_activity_time
            ).total_seconds() / 60
            protect_minutes = self._get_config("chat_protect_minutes", 10)
            if inactive_minutes < protect_minutes:
                remaining = protect_minutes - inactive_minutes
                chat_protection_text = f"距保护结束约 {int(remaining)} 分钟"
                response_parts.append(
                    f"\n🛡️ 聊天保护中：距最近对话模型活动还需 {int(remaining)} 分钟"
                )

        # Message queue stats
        queue_stats = self.interceptor.get_queue_stats()
        queued_messages = sum(
            int(stats.get("count", 0)) for stats in queue_stats.values()
        )
        if queue_stats:
            response_parts.append("\n📨 待处理消息：")
            for user_id, stats in queue_stats.items():
                response_parts.append(
                    f"  用户 {user_id[:8]}...：{stats['count']} 条消息"
                )

        try:
            png = self.image_renderer.render_status(
                BusyStatusImageData(
                    is_busy=self.busy_mgr.is_busy,
                    activity=activity,
                    remaining_minutes=remaining_mins,
                    current_start=current_start,
                    current_end=current_end,
                    next_busy=next_busy_text,
                    chat_protection=chat_protection_text,
                    queued_messages=queued_messages,
                    queued_users=len(queue_stats),
                ),
                now=self._now_in_astrbot_timezone(),
                mode=self._get_config("schedule_image_theme", "自动切换"),
            )
            yield event.chain_result([Image.fromBytes(png)])
        except Exception as exc:  # noqa: BLE001
            logger.exception("[BusySchedule] Status image rendering failed: %s", exc)
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
        data = active.data if active else None
        timeline = self._get_resolved_timeline(active.owner_date) if active else []
        current_period = next((item for item in timeline if item.contains(now)), None)
        if active:
            self._sync_media_execution(active, current_period)

        # Part 0: custom user-defined injection
        custom_text = self.injector.build_custom_injection()

        # Part 1: stable daily block.
        static_text = self.injector.build_static_injection(
            data, calendar_date=self._now_in_astrbot_timezone().date()
        )

        # Part 2: volatile blocks follow the same system_prompt order as requests.
        activity_text = (
            self.injector.build_activity_injection(data, timeline, now) if data else ""
        )
        execution_text = (
            self.injector.build_execution_injection(self.media_execution.to_dict())
            if active
            else ""
        )

        # Part 3: busy flag (only when busy)
        busy_text = ""
        if self.busy_mgr.is_busy and self.busy_mgr.current_activity:
            period = BusyPeriod(
                start_time="", end_time="", activity=self.busy_mgr.current_activity
            )
            busy_text = self.injector.build_busy_state_injection(period)

        # Build preview
        parts = [
            "=" * 30,
            "📋 提示词注入预览",
            "=" * 30,
            "",
            "【system_prompt 固定顺序】",
            "AstrBot 人设 → 今日日期/穿搭/天气/日程（同一静态块） → 自定义提示 → 下一个/当前活动 → 忙碌状态 → 内心世界锚点 → 执行记录",
            "",
            "[今日日期 · 今日穿搭 · 今日天气 · 今日日程]",
            static_text if static_text else "（日期注入已关闭，且无已完成日程）",
            "",
            "[自定义提示]",
            custom_text if custom_text else "（无自定义内容）",
            "",
            "[活动状态]",
            activity_text if activity_text else "（无活动状态）",
            "",
            "[忙碌状态]",
            busy_text if busy_text else "（当前不忙碌）",
            "",
            "[内心世界锚点]",
            "<!-- EMOTION_STATE_ANCHOR -->",
            "",
            "[执行记录]",
            execution_text if execution_text else "（未启用执行记录）",
            "",
            "延迟消息说明仅在真正发送延迟消息时作为临时 user 上下文注入，不写入历史。",
        ]

        parts.extend(["", "=" * 30])

        yield event.plain_result("\n".join(parts))
