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

from .core.data import ScheduleDataManager, ScheduleData, BusyPeriod
from .core.generator import ScheduleGenerator, _SCHEMA_DEFAULTS
from .core.busy_manager import BusyPeriodManager
from .core.message_interceptor import MessageInterceptor
from .core.prompt_injector import PromptInjector


@register(
    "astrbot_plugin_busy_schedule",
    "灵犀 · AI忙碌时段管理",
    "让AI拥有真实的生活节奏！自动计算忙碌时段、智能拦截合并消息、特殊关键词唤醒",
    "v1.3.2",
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

        # Peek timers: per-user tasks for random in-busy message delivery
        self._peek_timers: dict[str, asyncio.Task] = {}

        # Periodic poll task: background loop that fires while busy
        self._busy_poll_task: Optional[asyncio.Task] = None

    async def initialize(self):
        """Initialize plugin and all modules."""
        logger.info("[BusySchedule] Initializing plugin...")

        # Initialize modules
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
            if self.busy_mgr.is_busy:
                if has_queue and period:
                    await self._send_merged_messages(umo, period)
                await self.busy_mgr.wake_up("external")
            elif has_queue:
                _period = period or BusyPeriod(
                    start_time="??:??", end_time=datetime.now().strftime("%H:%M"), activity="忙碌时段"
                )
                await self._send_merged_messages(umo, _period)

        self.context._busy_schedule_wake_and_flush = _wake_and_flush

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

        # Cancel all message timers
        self.interceptor.cancel_all_timers()

        # Cancel all peek timers
        for task in list(self._peek_timers.values()):
            task.cancel()
        self._peek_timers.clear()

        # Cancel poll task
        if self._busy_poll_task and not self._busy_poll_task.done():
            self._busy_poll_task.cancel()
        self._busy_poll_task = None

        logger.info("[BusySchedule] Plugin terminated")

    async def _ensure_today_schedule_async(self):
        """Async wrapper for schedule generation with error handling."""
        try:
            await self._ensure_today_schedule()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[BusySchedule] Async schedule generation failed: {e}")

    async def _ensure_today_schedule(self):
        """Ensure today's schedule exists."""
        today = date.today()
        data = self.data_mgr.get(today)

        if not data or data.status != "completed":
            logger.info("[BusySchedule] Generating today's schedule...")
            try:
                await self.generator.generate_schedule(today)
            except Exception as e:
                logger.error(f"[BusySchedule] Failed to generate schedule: {e}")

        self._sync_schedule_to_context(today)

    def _sync_schedule_to_context(self, today: date):
        """Sync today's schedule data to context for downstream plugins."""
        custom_prompt = self._get_config("custom_prompt", "")
        self.context._busy_schedule_custom_prompt = custom_prompt or ""
        data = self.data_mgr.get(today)
        if data and data.status == "completed":
            self.context._busy_schedule_today_schedule = data.schedule
            self.context._busy_schedule_outfit = data.outfit or ""
            now = datetime.now()
            current = self.injector._find_current_activity(data, now)
            self.context._busy_schedule_current_activity = current or ""
            next_act = ""
            next_start = ""
            if data.busy_periods:
                for period in sorted(data.busy_periods, key=lambda p: p.start_time):
                    if period.start_datetime > now:
                        next_act = period.activity
                        next_start = period.start_time
                        break
            self.context._busy_schedule_next_activity = f"{next_act}（{next_start}开始）" if next_act else ""
        else:
            self.context._busy_schedule_today_schedule = ""
            self.context._busy_schedule_outfit = ""
            self.context._busy_schedule_current_activity = ""
            self.context._busy_schedule_next_activity = ""

    async def _daily_refresh_loop(self):
        """Background loop that waits until schedule_time each day, then refreshes."""
        while True:
            try:
                schedule_time_str = self._get_config("schedule_time", "07:00")
                hour, minute = 0, 0
                try:
                    parts = schedule_time_str.split(":")
                    hour = int(parts[0])
                    minute = int(parts[1])
                except Exception:
                    logger.warning(f"[BusySchedule] Invalid schedule_time: {schedule_time_str}, defaulting to 07:00")
                    hour, minute = 7, 0

                now = datetime.now()
                target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

                if target <= now:
                    target += timedelta(days=1)

                wait_seconds = (target - now).total_seconds()
                logger.info(f"[BusySchedule] Next schedule refresh at {target.strftime('%Y-%m-%d %H:%M')}, waiting {wait_seconds:.0f}s")
                await asyncio.sleep(wait_seconds)

                # Time to refresh
                logger.info(f"[BusySchedule] Daily refresh triggered at {datetime.now().strftime('%H:%M')}")
                today = date.today()
                try:
                    await self.generator.generate_schedule(today)
                    logger.info(f"[BusySchedule] Daily schedule refreshed for {today}")
                except Exception as e:
                    logger.error(f"[BusySchedule] Daily refresh failed: {e}")

                self._sync_schedule_to_context(today)

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
                self._sync_schedule_to_context(date.today())
                if self.busy_mgr.is_busy:
                    logger.info(
                        f"[BusySchedule] State sync: is_busy=True, "
                        f"manual_period={self.busy_mgr._current_busy_period is not None}, "
                        f"cooldown={self.busy_mgr._is_in_wakeup_cooldown(datetime.now())}"
                    )

    async def _on_enter_busy(self, period: BusyPeriod):
        """Callback when entering busy state."""
        logger.info(f"[BusySchedule] Entering busy: {period.activity}")
        self.context._busy_schedule_is_busy = True

        # Start periodic poll loop if enabled
        if self._get_config("poll_enabled", False):
            if self._busy_poll_task and not self._busy_poll_task.done():
                self._busy_poll_task.cancel()
            self._busy_poll_task = asyncio.create_task(self._busy_poll_loop(period))

    async def _on_exit_busy(self, period: BusyPeriod):
        """Callback when exiting busy state."""
        logger.info(f"[BusySchedule] Exiting busy: {period.activity}")
        self.context._busy_schedule_is_busy = False

        # Stop poll loop
        if self._busy_poll_task and not self._busy_poll_task.done():
            self._busy_poll_task.cancel()
        self._busy_poll_task = None

        # Process queued messages for all users with random delay
        user_ids = self.interceptor.get_all_queued_user_ids()
        for user_id in user_ids:
            if self.interceptor.has_queued_messages(user_id):
                if user_id in self._peek_timers and not self._peek_timers[user_id].done():
                    # Peek timer is running — it will send the messages, skip
                    logger.info(f"[BusySchedule] Peek timer active for {user_id}, skipping exit send")
                    continue
                asyncio.create_task(self._delayed_send(user_id, period))

    async def _delayed_send(self, user_id: str, period: BusyPeriod):
        """Send merged messages after a random delay following busy exit."""
        delay_min = self._get_config("exit_delay_min_seconds", 10)
        delay_max = self._get_config("exit_delay_max_seconds", 120)
        delay = random.uniform(delay_min, delay_max)
        logger.info(f"[BusySchedule] Delayed send for {user_id}: {delay:.1f}s")
        await asyncio.sleep(delay)
        if self.interceptor.has_queued_messages(user_id):
            await self._send_merged_messages(user_id, period)

    def _start_peek_timer(self, user_id: str, period: BusyPeriod):
        """Start a peek timer that fires after a random delay to send queued messages early."""
        self._cancel_peek_timer(user_id)
        delay_min = self._get_config("peek_delay_min_seconds", 5)
        delay_max = self._get_config("peek_delay_max_seconds", 30)
        delay = random.uniform(delay_min, delay_max)
        logger.info(f"[BusySchedule] Peek timer started for {user_id}: {delay:.1f}s")

        async def _callback():
            await asyncio.sleep(delay)
            self._peek_timers.pop(user_id, None)
            if self.interceptor.has_queued_messages(user_id):
                await self._send_merged_messages(user_id, period)

        self._peek_timers[user_id] = asyncio.create_task(_callback())

    def _cancel_peek_timer(self, user_id: str):
        """Cancel an active peek timer for a user."""
        task = self._peek_timers.pop(user_id, None)
        if task and not task.done():
            task.cancel()

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
                    prob = float(prob_str)
                    mn, mx = interval_str.split("-")
                    return prob, float(mn), float(mx)
                except Exception:
                    continue
        # Global defaults
        prob = self._get_config("poll_probability", 0.3)
        mn = self._get_config("poll_interval_min_minutes", 5)
        mx = self._get_config("poll_interval_max_minutes", 15)
        return float(prob), float(mn), float(mx)

    async def _busy_poll_loop(self, period: BusyPeriod):
        """Background loop that fires periodically while busy and may send queued messages."""
        try:
            while self.busy_mgr.is_busy:
                activity = self.busy_mgr.current_activity or ""
                prob, mn_min, mx_min = self._get_poll_params(activity)
                wait_seconds = random.uniform(mn_min * 60, mx_min * 60)
                logger.info(
                    f"[BusySchedule] Poll loop: next check in {wait_seconds:.0f}s "
                    f"(activity={activity!r}, prob={prob})"
                )
                await asyncio.sleep(wait_seconds)

                if not self.busy_mgr.is_busy:
                    break

                if random.random() >= prob:
                    continue

                # Triggered — send queued messages for all users (skip if peek already handling)
                quiet = self._get_config("poll_quiet_seconds", 30)
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
                    current_period = self.busy_mgr._current_busy_period or period
                    logger.info(f"[BusySchedule] Poll triggered send for {user_id}")
                    asyncio.create_task(self._delayed_send(user_id, current_period))
        except asyncio.CancelledError:
            pass

    async def _send_merged_messages(self, user_id: str, period: BusyPeriod):
        """Send merged messages after busy period ends by re-injecting into pipeline."""
        merged_text = self.interceptor.get_merged_message(
            user_id,
            period.start_time,
            period.end_time,
        )

        if not merged_text:
            return

        extra_components = self.interceptor.get_extra_components(user_id)

        # Get stored event reference
        events = self.interceptor._event_refs.get(user_id, [])
        if not events:
            logger.warning(f"[BusySchedule] No event ref for {user_id}, cannot send merged messages")
            self.interceptor.mark_sent(user_id)
            return

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

        # Clean up queue and refs before re-injection
        self.interceptor.mark_sent(user_id)

        # Re-inject into event queue
        try:
            self.context.get_event_queue().put_nowait(reinjected_event)
        except Exception as e:
            logger.error(f"[BusySchedule] Failed to re-inject event for {user_id}: {e}")

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

    def _get_config(self, key: str, default=None):
        """Get config value with schema default fallback."""
        # Nested groups take priority — user-edited values live here
        for group_name in ["基础设置", "忙碌时段", "关键词设置", "消息合并", "智能判断", "日程生成"]:
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
                # Queue the wake keyword message itself so it's included in the merged batch
                self.interceptor.queue_message(user_id, message_text, event, extra_components=extra_comps)
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
                # Peek: randomly deliver queued messages early while still busy
                if self._get_config("peek_enabled", False):
                    if user_id in self._peek_timers and not self._peek_timers[user_id].done():
                        # Reset timer so new message is included
                        period = self.busy_mgr._current_busy_period or BusyPeriod(
                            start_time="??:??", end_time=datetime.now().strftime("%H:%M"), activity="忙碌时段"
                        )
                        self._start_peek_timer(user_id, period)
                    elif random.random() < self._get_config("peek_probability", 0.05):
                        period = self.busy_mgr._current_busy_period or BusyPeriod(
                            start_time="??:??", end_time=datetime.now().strftime("%H:%M"), activity="忙碌时段"
                        )
                        self._start_peek_timer(user_id, period)
            elif result == "force_send":
                # Will be handled by timer or next state check
                event.stop_event()
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

        today = date.today()
        data = self.data_mgr.get(today)

        if not data or data.status != "completed":
            return

        now = datetime.now()

        # Part 1 + 2: static (outfit+schedule) + semi-static (current/next activity)
        static_injection = self.injector.build_static_injection(data)
        schedule_injection = self.injector.build_schedule_injection(data, now)
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
        today = date.today()
        today_str = today.strftime("%Y-%m-%d")
        data = self.data_mgr.get(today)

        if not data or data.status != "completed":
            yield event.plain_result("今日日程尚未生成，正在生成...")
            try:
                data = await self.generator.generate_schedule_or_wait(today, umo=event.unified_msg_origin)
            except Exception as e:
                yield event.plain_result(f"日程生成失败：{e}")
                return

        # Build response
        response_parts = [
            f"📅 {today_str}",
            "",
            f"👗 今日穿搭：{data.outfit}",
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
        today = date.today()

        if extra:
            yield event.plain_result(f"正在根据补充要求重写今日日程：{extra}")
        else:
            yield event.plain_result("正在重写今日日程...")

        try:
            data = await self.generator.generate_schedule(today, umo=event.unified_msg_origin, extra=extra if extra else None)
            yield event.plain_result(
                f"📅 {today.strftime('%Y-%m-%d')}\n"
                f"👗 今日穿搭：{data.outfit}\n"
                f"📝 日程安排：\n{data.schedule}"
            )

            self._sync_schedule_to_context(today)

            # Refresh busy state so current_busy_period matches new schedule
            if self.busy_mgr.is_busy:
                now = datetime.now()
                new_period = self.busy_mgr.get_current_busy_period(now)
                if new_period:
                    self.busy_mgr._current_busy_period = new_period
                    logger.info(f"[BusySchedule] Refreshed busy period after rewrite: {new_period.activity}")
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
        today = date.today()
        data = self.data_mgr.get(today)

        response_parts = ["📊 忙碌状态信息", ""]

        # Current status
        if self.busy_mgr.is_busy:
            activity = self.busy_mgr.current_activity or "未知活动"
            response_parts.append(f"💤 当前状态：忙碌中（{activity}）")
            # Show remaining time in minutes
            current_period = self.busy_mgr._current_busy_period
            if current_period:
                end_dt = current_period.end_datetime
                remaining_secs = (end_dt - now).total_seconds()
                if remaining_secs > 0:
                    remaining_mins = int(remaining_secs / 60)
                    response_parts.append(f"⏱️ 剩余时间：约 {remaining_mins} 分钟")
        else:
            response_parts.append("✅ 当前状态：在线")

        # Next busy period
        next_period = self.busy_mgr.get_next_busy_period(now)
        if next_period:
            response_parts.append(
                f"\n⏰ 下一个忙碌时段：{next_period.start_time}-{next_period.end_time} {next_period.activity}"
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
• 立刻判断 / busy judge - 立刻触发智能判断（检查是否需要调整日程）
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

    @filter.command("立刻判断", alias={"busy judge"})
    async def cmd_judge_now(self, event: AstrMessageEvent, extra: str = ""):
        """立刻触发智能判断（检查是否需要调整日程）"""
        if not self._get_config("enable_smart_judge", False):
            yield event.plain_result("智能判断功能未启用，请在配置中开启 enable_smart_judge")
            return

        today = date.today()
        data = self.data_mgr.get(today)

        if not data or data.status != "completed":
            yield event.plain_result("今日日程尚未生成，请先执行「忙碌日程」生成日程")
            return

        user_id = event.unified_msg_origin
        user_message = extra if extra else "用户请求立刻判断日程是否需要调整"

        yield event.plain_result("正在进行智能判断...")

        try:
            result = await self.generator.judge_and_adjust_schedule(today, user_message, umo=user_id)
        except Exception as e:
            logger.error(f"[BusySchedule] cmd_judge_now exception: {e}")
            yield event.plain_result(f"判断异常：{e}")
            return

        if not result:
            yield event.plain_result("判断失败（返回空），请查看控制台日志中 [BusySchedule] 相关警告")
            return

        if not result.get("adjusted"):
            reason = result.get("reason", "无需调整")
            yield event.plain_result(f"判断结果：{reason}")
            return

        # Show adjustments
        adjustments = result.get("adjustments", [])
        reason = result.get("reason", "")
        adj_desc = []
        for adj in adjustments:
            original = adj.get("original_activity", "")
            new = adj.get("new_activity", original)
            original_time = adj.get("original_time", "")
            new_time = adj.get("new_time", original_time)
            if original != new or original_time != new_time:
                adj_desc.append(f"{original_time} {original} -> {new_time} {new}")

        if adj_desc:
            yield event.plain_result(f"✅ 日程已调整：\n" + "\n".join(adj_desc) + f"\n原因：{reason}")
        else:
            yield event.plain_result(f"判断结果：{reason}")

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
        today = date.today()
        data = self.data_mgr.get(today)

        if not data or data.status != "completed":
            yield event.plain_result("今日日程尚未生成，请先执行「忙碌日程」生成日程")
            return

        now = datetime.now()

        # Part 0: custom user-defined injection
        custom_text = self.injector.build_custom_injection()

        # Part 1: static (outfit + schedule)
        static_text = self.injector.build_static_injection(data)

        # Part 2: semi-static (current + next activity)
        schedule_text = self.injector.build_schedule_injection(data, now)

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