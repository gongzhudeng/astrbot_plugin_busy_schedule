"""Schedule generator module - generates daily schedule with busy period markers."""

import asyncio
import json
import random
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context

from .data import (
    BusyPeriod,
    ScheduleData,
    ScheduleDataManager,
    parse_schedule_time,
)
from .history_content import (
    extract_history_text,
    find_datetime_reminder,
)
from .weather import WeatherService, WeatherSnapshot

try:
    from astrbot.core.cron.events import CronMessageEvent
    from astrbot.core.pipeline.context import call_event_hook
    from astrbot.core.platform.message_session import MessageSession
    from astrbot.core.provider.entities import ProviderRequest
    from astrbot.core.star.star_handler import EventType

    HAS_PIPELINE = True
except ImportError:
    HAS_PIPELINE = False


def _load_schema_defaults() -> dict:
    """Load default values from _conf_schema.json."""
    schema_path = Path(__file__).parent.parent / "_conf_schema.json"
    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        defaults = {}
        for group_name, group in schema.items():
            if not isinstance(group, dict):
                continue
            items = group.get("items", {})
            if not isinstance(items, dict):
                continue
            for key, field in items.items():
                if isinstance(field, dict) and "default" in field:
                    defaults[key] = field["default"]
                # Handle nested items (e.g., pool -> daily_themes)
                if isinstance(field, dict) and "items" in field:
                    nested = {}
                    for nk, nv in field["items"].items():
                        if isinstance(nv, dict) and "default" in nv:
                            nested[nk] = nv["default"]
                    if nested:
                        defaults[key] = nested
        return defaults
    except Exception as e:
        logger.warning(f"[BusySchedule] Failed to load schema defaults: {e}")
        return {}


_SCHEMA_DEFAULTS = _load_schema_defaults()


def get_holiday(date_obj: date) -> str:
    """Get holiday name for a date. Uses holidays lib with fallback dict."""
    # Chinese holidays via python-holidays
    try:
        import holidays as _holidays

        cn_holidays = _holidays.CN()
        name = cn_holidays.get(date_obj)
        if name:
            return name
    except Exception:
        pass

    # Fallback: common holidays not always in the lib
    _EXTRA_HOLIDAYS = {
        (2, 14): "情人节",
        (3, 8): "妇女节",
        (3, 14): "白色情人节",
        (5, 20): "网络情人节",
        (6, 1): "儿童节",
        (8, 14): "绿色情人节",
        (10, 31): "万圣节",
        (11, 11): "双十一",
        (12, 24): "平安夜",
        (12, 25): "圣诞节",
    }
    return _EXTRA_HOLIDAYS.get((date_obj.month, date_obj.day), "")


def get_weekday_cn(date_obj: date) -> str:
    """Get Chinese weekday name."""
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return weekdays[date_obj.weekday()]


def _extract_json_obj(text: str) -> Optional[dict]:
    """Extract JSON object from text using robust bracket matching.

    Handles markdown code blocks, nested objects, and string escaping.
    """
    text = text.strip()
    # Remove markdown code block markers
    text = re.sub(r"^```json\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)

    start = text.find("{")
    if start == -1:
        return None

    brace = 0
    in_string = False
    escape = False

    for i, ch in enumerate(text[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                brace += 1
            elif ch == "}":
                brace -= 1
                if brace == 0:
                    json_str = text[start : i + 1]
                    try:
                        data = json.loads(json_str)
                        return data if isinstance(data, dict) else None
                    except Exception:
                        return None
    return None


def _extract_completion_text(resp: object) -> str:
    """Extract completion text from LLM response object."""
    if resp is None:
        return ""
    for key in ("completion_text", "completion", "text", "content"):
        value = getattr(resp, key, None)
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    return ""


class DeterministicScheduleError(RuntimeError):
    """Raised when an LLM response still violates the schedule protocol."""


class ScheduleGenerator:
    """Generates daily schedule with busy period markers."""

    _LLM_CALL_ATTEMPTS = 3
    _FORMAT_REPAIR_ATTEMPTS = 1

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig,
        data_mgr: ScheduleDataManager,
        weather_service: Optional[WeatherService] = None,
    ):
        self.context = context
        self.config = config
        self.data_mgr = data_mgr
        self.weather_service = weather_service
        self._generating = False
        self._generation_future: Optional[asyncio.Future] = None
        self._generation_target: Optional[date] = None

    def _cfg(self, key: str, default=None):
        """Get config value with schema default fallback."""
        # Nested groups take priority — user-edited values live here
        for group_name in [
            "基础设置",
            "忙碌时段",
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

        schema_default = _SCHEMA_DEFAULTS.get(key)
        if schema_default is not None:
            return schema_default
        return default

    def _get_provider(self):
        """Get LLM provider for schedule generation."""
        provider_id = self._cfg("llm_provider_schedule", "")
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider:
                return provider
        return self.context.get_using_provider()

    async def _get_persona_desc(self, umo: Optional[str] = None) -> str:
        """Get bot persona description for schedule generation.

        Priority: explicit config persona > current conversation persona > system default > fallback.
        """
        try:
            # 1. Explicit persona configured in plugin settings
            persona_id = self._cfg("schedule_persona_id", "")

            # 2. Current conversation persona
            if not persona_id and umo:
                try:
                    conv_mgr = self.context.conversation_manager
                    if conv_mgr:
                        cid = await conv_mgr.get_curr_conversation_id(umo)
                        if cid:
                            conv = await conv_mgr.get_conversation(umo, cid)
                            if conv and getattr(conv, "persona_id", None):
                                persona_id = conv.persona_id
                except Exception:
                    pass

            # Look up persona system_prompt by id
            if persona_id:
                persona_mgr = self.context.persona_manager
                if persona_mgr:
                    for persona in persona_mgr.personas:
                        if persona.persona_id == persona_id:
                            return (
                                persona.system_prompt[:500]
                                if persona.system_prompt
                                else ""
                            )

            # 3. System default persona via persona_manager
            try:
                p = await self.context.persona_manager.get_default_persona_v3()
                if isinstance(p, dict) and p.get("prompt"):
                    return p["prompt"][:500]
                if hasattr(p, "prompt") and p.prompt:
                    return p.prompt[:500]
            except Exception:
                pass

            # 4. Fallback
            return "一个活泼可爱的AI助手"
        except Exception:
            return "一个活泼可爱的AI助手"

    def _get_history_schedules(self, target_date: date, days: int = 3) -> str:
        """Get recent history schedules for reference."""
        history = []
        for i in range(1, days + 1):
            past_date = target_date - timedelta(days=i)
            data = self.data_mgr.get(past_date)
            if not data or not data.schedule:
                continue
            style = (data.outfit_style or "").strip()
            outfit = data.outfit[:40] if data.outfit else ""
            schedule = data.schedule[:60]
            if style:
                history.append(
                    f"[{past_date.strftime('%Y-%m-%d')}] 风格：{style} 穿搭：{outfit} 日程：{schedule}"
                )
            else:
                history.append(
                    f"[{past_date.strftime('%Y-%m-%d')}] 穿搭：{outfit} 日程：{schedule}"
                )
        return "\n".join(history) if history else "无历史日程"

    def _get_yesterday_last_activity(self, target_date: date) -> str:
        """Get the last activity entry from yesterday's schedule.

        Returns the last line of yesterday's schedule (e.g. "23:00-07:00 睡觉 【忙碌】"),
        or empty string if not found. This is used for schedule continuity so today's
        schedule starts from waking up after yesterday's last activity.
        """
        yesterday = target_date - timedelta(days=1)
        data = self.data_mgr.get(yesterday)
        if not data or not data.schedule:
            return ""

        lines = [
            line.strip() for line in data.schedule.strip().split("\n") if line.strip()
        ]
        if not lines:
            return ""

        last_line = lines[-1]
        logger.info(f"[BusySchedule] Yesterday's last activity: {last_line}")
        return last_line

    async def _get_recent_chats(
        self, umo: Optional[str] = None, rounds: Optional[int] = None
    ) -> str:
        """Get recent conversation rounds for schedule generation."""
        if rounds is None:
            rounds = int(self._cfg("reference_recent_count", 10))
        if not umo or rounds <= 0:
            return "无近期对话"

        contexts = await self._get_conversation_contexts(
            umo,
            rounds,
            include_datetime=True,
        )
        if not contexts:
            return "无近期对话记录"

        formatted = []
        for msg in contexts:
            raw_content = msg.get("content", "")
            content = extract_history_text(raw_content, exclude_datetime=True)
            if not content:
                continue
            if len(content) > 200:
                content = content[:200] + "..."
            reminder = find_datetime_reminder(raw_content)
            if reminder:
                content = f"{content} {reminder}"
            speaker = "用户" if msg.get("role") == "user" else "我"
            formatted.append(f"{speaker}: {content}")

        logger.info(
            f"[BusySchedule] Recent chats selected: rounds={rounds}, "
            f"messages={len(contexts)}, chars={sum(len(item) for item in formatted)}"
        )
        return "\n".join(formatted) if formatted else "无近期对话记录"

    async def _get_conversation_contexts(
        self,
        umo: str,
        rounds: int,
        *,
        include_datetime: bool = False,
    ) -> list[dict]:
        """Fetch recent rounds projected for model context or semantic retrieval."""
        if rounds <= 0:
            return []
        try:
            conv_mgr = self.context.conversation_manager
            if not conv_mgr:
                return []
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            if not curr_cid:
                return []
            conversation = await conv_mgr.get_conversation(umo, curr_cid)
            if not conversation or not conversation.history:
                return []

            history = (
                json.loads(conversation.history)
                if isinstance(conversation.history, str)
                else conversation.history
            )
            if not isinstance(history, list):
                return []

            messages = []
            user_rounds = 0
            for msg in reversed(history):
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                raw_content = msg.get("content", "")
                content = (
                    raw_content
                    if include_datetime
                    else extract_history_text(
                        raw_content,
                        exclude_datetime=True,
                    )
                )
                if not extract_history_text(content):
                    continue
                messages.append({"role": role, "content": content})
                if role == "user":
                    user_rounds += 1
                    if user_rounds >= rounds:
                        break

            messages.reverse()
            return messages
        except Exception as e:
            logger.warning(f"[BusySchedule] Failed to get conversation contexts: {e}")
            return []

    @staticmethod
    def _format_retrieval_query(contexts: list[dict], max_chars: int) -> str:
        """Format contexts while preserving the start of every selected message."""
        lines = []
        for msg in contexts:
            content = str(msg.get("content", "")).strip()
            if not content:
                continue
            speaker = "用户" if msg.get("role") == "user" else "我"
            lines.append(f"{speaker}: {content}")

        query = "\n".join(lines)
        if max_chars <= 0 or len(query) <= max_chars:
            return query

        separator_chars = len(lines) - 1
        content_budget = max_chars - separator_chars
        if content_budget <= 0:
            return query[:max_chars]

        per_line, remainder = divmod(content_budget, len(lines))
        truncated = [
            line[: per_line + (index < remainder)] for index, line in enumerate(lines)
        ]
        return "\n".join(truncated)

    @staticmethod
    def _text_from_content_part(part) -> str:
        """Extract text from an AstrBot content part or compatible object."""
        if isinstance(part, str):
            return part
        if isinstance(part, dict):
            return str(part.get("text") or part.get("content") or "")
        return str(getattr(part, "text", "") or getattr(part, "content", ""))

    def _collect_rag_results(self, event, req) -> list[str]:
        """Collect explicit background retrieval results with legacy fallbacks."""
        explicit = event.get_extra("background_retrieval_results") or []
        results = [str(item).strip() for item in explicit if str(item).strip()]
        if results:
            return list(dict.fromkeys(results))

        for part in getattr(req, "extra_user_content_parts", []) or []:
            text = self._text_from_content_part(part).strip()
            if text:
                results.append(text)

        for context in getattr(req, "contexts", []) or []:
            text = self._text_from_content_part(context).strip()
            if text and ("RAG" in text or "记忆" in text or "知识库" in text):
                results.append(text)

        system_prompt = (getattr(req, "system_prompt", "") or "").strip()
        if system_prompt and "BUSY_SCHEDULE_" not in system_prompt:
            results.append(system_prompt)

        return list(dict.fromkeys(results))

    async def _get_rag_context(self, umo: Optional[str] = None) -> str:
        """Query memory and knowledge plugins with a pure recent-chat query."""
        if (
            not HAS_PIPELINE
            or not self._cfg("enable_rag_for_schedule", True)
            or not umo
        ):
            return ""

        try:
            rounds = int(self._cfg("rag_query_rounds", 5))
            query_max = max(0, int(self._cfg("rag_query_max_chars", 500)))
            result_max = max(0, int(self._cfg("rag_result_max_chars", 800)))
            if rounds <= 0:
                return ""

            contexts = await self._get_conversation_contexts(umo, rounds)
            if not contexts:
                return ""

            query = self._format_retrieval_query(contexts, query_max)
            if not query:
                return ""

            logger.info(
                f"[BusySchedule] RAG query prepared: rounds={rounds}, "
                f"messages={len(contexts)}, chars={len(query)}"
            )
            session = MessageSession.from_str(umo)
            cron_event = CronMessageEvent(
                context=self.context,
                session=session,
                message=query,
                extras={
                    "background_retrieval": True,
                    "retrieval_query": query,
                    "background_retrieval_results": [],
                },
            )
            req = ProviderRequest()
            req.prompt = query
            req.system_prompt = ""

            await call_event_hook(cron_event, EventType.OnLLMRequestEvent, req)

            results = self._collect_rag_results(cron_event, req)
            if not results:
                logger.info("[BusySchedule] RAG hooks returned no usable context")
                return ""

            injected = "\n\n".join(results)
            if result_max > 0 and len(injected) > result_max:
                injected = injected[:result_max]
            logger.info(
                f"[BusySchedule] RAG context collected: sources={len(results)}, "
                f"chars={len(injected)}"
            )
            return injected

        except Exception as e:
            logger.warning(f"[BusySchedule] _get_rag_context failed: {e}")
            return ""

    async def _build_prompt(
        self,
        target_date: date,
        extra: Optional[str] = None,
        umo: Optional[str] = None,
        weather: Optional[WeatherSnapshot] = None,
    ) -> str:
        """Build the prompt for schedule generation.

        Uses str.format() with double-brace escaping for JSON output format.
        """
        template = self._cfg("prompt_template", "")
        if not template:
            raise RuntimeError("prompt_template is empty")

        pool = self._cfg("pool", {})
        daily_themes = pool.get("daily_themes", [])
        mood_colors = pool.get("mood_colors", [])
        outfit_styles = pool.get("outfit_styles", [])
        schedule_types = pool.get("schedule_types", [])

        ctx = {
            "date_str": target_date.strftime("%Y年%m月%d日"),
            "weekday": get_weekday_cn(target_date),
            "holiday": get_holiday(target_date) or "普通日子",
            "persona_desc": await self._get_persona_desc(umo),
            "daily_theme": random.choice(daily_themes) if daily_themes else "随性日",
            "mood_color": random.choice(mood_colors) if mood_colors else "随性",
            "outfit_style": random.choice(outfit_styles) if outfit_styles else "休闲风",
            "schedule_type": random.choice(schedule_types)
            if schedule_types
            else "随性漫游型",
            "history_schedules": self._get_history_schedules(target_date),
            "last_yesterday_activity": self._get_yesterday_last_activity(target_date),
            "recent_chats": await self._get_recent_chats(umo),
            "rag_context": await self._get_rag_context(umo),
            "weather_forecast": (
                weather.format_for_prompt()
                if weather
                else "天气预报暂不可用。请按其他上下文正常规划，不要虚构具体天气。"
            ),
        }

        try:
            prompt = template.format(**ctx)
        except KeyError as e:
            logger.warning(
                f"[BusySchedule] prompt_template has unknown placeholder: {e}"
            )
            # Fallback: fill what we can
            prompt = template
            for k, v in ctx.items():
                prompt = prompt.replace(f"{{{k}}}", str(v))

        if extra:
            prompt += f"\n\n## 用户补充要求\n{extra}"

        return prompt

    @staticmethod
    def _normalize_schedule_text(schedule_text: str) -> str:
        """Remove only known legacy explanatory lines from generated schedules."""
        legacy_note_pattern = re.compile(
            r"^[（(]?未标注(?:的)?时(?:间)?段(?:默认|默认为)?(?:小怡)?(?:在)?"
            r"(?:玩手机|赖床|无所事事|宿舍无所事事)"
            r"(?:\s*[/／或、]\s*(?:玩手机|赖床|无所事事|宿舍无所事事))*"
            r"[。.]?[）)]?$"
        )
        normalized_lines = []
        removed_lines = []
        for raw_line in schedule_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if legacy_note_pattern.fullmatch(line):
                removed_lines.append(line)
                continue
            normalized_lines.append(line)

        if removed_lines:
            logger.info(
                "[BusySchedule] Removed known legacy schedule note: "
                + " | ".join(removed_lines)
            )
        return "\n".join(normalized_lines)

    def _parse_busy_periods_from_schedule(self, schedule_text: str) -> list[BusyPeriod]:
        """Parse ranged activities and one final open-ended sleep entry."""
        schedule_text = self._normalize_schedule_text(schedule_text)
        lines = [line.strip() for line in schedule_text.splitlines() if line.strip()]
        line_pattern = re.compile(
            r"^(\d{1,2}:\d{2})(?:\s*[-~到至]\s*(\d{1,2}:\d{2}))?\s+(.+)$"
        )
        sleep_pattern = re.compile(r"睡觉|入睡|就寝|睡眠|午睡|小睡")
        periods = []

        for index, line in enumerate(lines):
            match = line_pattern.match(line)
            if not match:
                raise ValueError(f"无法解析日程行：{line}")

            start_time, end_time = match.group(1), match.group(2)
            activity_text = match.group(3).strip()
            marker_match = re.search(r"\s*【(忙碌|可回消息)】$", activity_text)
            marker = marker_match.group(1) if marker_match else None
            activity = (
                activity_text[: marker_match.start()].strip()
                if marker_match
                else activity_text
            )
            is_last = index == len(lines) - 1
            has_sleep_semantics = bool(sleep_pattern.search(activity))

            if end_time is None:
                if not has_sleep_semantics:
                    raise ValueError(
                        f"普通活动缺少结束时间：{line}；"
                        "普通活动必须使用 HH:MM-HH:MM，只有最后一条睡觉可以省略结束时间"
                    )
                if not is_last:
                    raise ValueError(f"开放睡眠只能放在日程最后一项：{line}")
                if marker != "忙碌":
                    raise ValueError(f"开放睡眠必须标记为【忙碌】：{line}")
                is_sleep = True
            else:
                is_sleep = False

            is_busy = marker != "可回消息"
            if marker is None and not is_sleep:
                non_busy_keywords = [
                    "刷手机",
                    "休息",
                    "散步",
                    "闲逛",
                    "发呆",
                    "赖床",
                    "看剧",
                    "玩",
                ]
                is_busy = not any(kw in activity for kw in non_busy_keywords)

            periods.append(
                BusyPeriod(
                    start_time=start_time,
                    end_time=end_time,
                    activity=activity,
                    is_busy=is_busy,
                    period_type="sleep" if is_sleep else "activity",
                )
            )

        if len(periods) < 2:
            raise ValueError("日程必须包含至少一条普通活动和最后一条睡觉活动")
        if not periods[-1].is_open_sleep:
            raise ValueError("日程最后一项必须是没有结束时间的睡眠活动")
        return periods

    @staticmethod
    def _validate_period_order(
        periods: list[BusyPeriod], target_date: date, schedule_time: tuple[int, int]
    ) -> None:
        """Validate period order on the cycle's absolute timeline."""
        starts = []
        for period in periods:
            hour, minute = map(int, period.start_time.split(":"))
            base = (
                target_date + timedelta(days=1)
                if (hour, minute) < schedule_time
                else target_date
            )
            starts.append(datetime(base.year, base.month, base.day, hour, minute))
        if starts != sorted(starts):
            raise ValueError("日程活动没有按周期内的绝对时间排序")
        first_ordinary = next(period for period in periods if not period.is_open_sleep)
        first_hour, first_minute = map(int, first_ordinary.start_time.split(":"))
        if (first_hour, first_minute) < schedule_time:
            raise ValueError("日程第一条普通活动不得早于日程生成时间")

    async def _cleanup_session(self, session_id: str):
        """Clean up LLM session."""
        try:
            conv_mgr = self.context.conversation_manager
            if conv_mgr:
                cid = await conv_mgr.get_curr_conversation_id(session_id)
                if cid:
                    await conv_mgr.delete_conversation(session_id, cid)
        except Exception:
            pass

    async def _call_llm(
        self, prompt: str, provider, session_id: str, system_prompt: str = ""
    ) -> str:
        """Call LLM and retry only transport failures or empty responses."""
        for attempt in range(self._LLM_CALL_ATTEMPTS):
            try:
                resp = await provider.text_chat(
                    prompt=prompt,
                    session_id=session_id,
                    system_prompt=system_prompt or None,
                )
                text = _extract_completion_text(resp)
                if text:
                    return text
                logger.warning(
                    f"[BusySchedule] Empty LLM response "
                    f"(attempt {attempt + 1}/{self._LLM_CALL_ATTEMPTS})"
                )
            except Exception as e:
                logger.warning(
                    f"[BusySchedule] LLM call failed "
                    f"(attempt {attempt + 1}/{self._LLM_CALL_ATTEMPTS}): {e}"
                )
            finally:
                await self._cleanup_session(session_id)
                session_id = f"busy_schedule_{uuid.uuid4().hex[:8]}"

        raise RuntimeError("LLM returned empty response after all call attempts")

    async def generate_schedule_or_wait(
        self, target_date: date, umo: Optional[str] = None, extra: Optional[str] = None
    ) -> ScheduleData:
        """Generate a schedule while sharing an in-flight target transaction."""
        return await self.generate_schedule(target_date, umo, extra)

    async def generate_schedule(
        self, target_date: date, umo: Optional[str] = None, extra: Optional[str] = None
    ) -> ScheduleData:
        """Generate schedule for a specific date."""
        if self._generating and self._generation_future:
            if self._generation_target != target_date:
                raise RuntimeError(
                    f"Schedule generation already in progress for {self._generation_target}"
                )
            logger.info("[BusySchedule] Waiting for in-progress generation...")
            return await asyncio.shield(self._generation_future)

        self._generating = True
        self._generation_target = target_date
        self._generation_future = asyncio.get_running_loop().create_future()
        try:
            schedule_time = parse_schedule_time(self._cfg("schedule_time", "07:00"))
            weather = None
            weather_service = getattr(self, "weather_service", None)
            if weather_service:
                try:
                    weather = await weather_service.get_forecast(
                        target_date, schedule_time
                    )
                except Exception as exc:
                    logger.warning(
                        f"[BusySchedule] Weather unavailable for generation: {exc}"
                    )
            prompt = await self._build_prompt(target_date, extra, umo, weather=weather)
            provider = self._get_provider()

            if not provider:
                raise RuntimeError("No LLM provider available")

            logger.info(f"[BusySchedule] Generating schedule for {target_date}")

            sid = f"busy_schedule_gen_{target_date.strftime('%Y%m%d')}_0"
            content = await self._call_llm(prompt, provider, sid)

            result = _extract_json_obj(content)
            periods = []
            validation_error = ""

            for repair_attempt in range(self._FORMAT_REPAIR_ATTEMPTS + 1):
                if result and isinstance(result.get("schedule"), str):
                    try:
                        normalized_schedule = self._normalize_schedule_text(
                            result["schedule"]
                        )
                        periods = self._parse_busy_periods_from_schedule(
                            normalized_schedule
                        )
                        self._validate_period_order(
                            periods,
                            target_date,
                            schedule_time,
                        )
                        result["schedule"] = normalized_schedule
                        validation_error = ""
                        break
                    except (TypeError, ValueError) as exc:
                        validation_error = str(exc)

                reason = (
                    "未能解析出 JSON 对象"
                    if not result
                    else validation_error or "schedule 字段为空或类型错误"
                )
                if repair_attempt >= self._FORMAT_REPAIR_ATTEMPTS:
                    break

                logger.warning(
                    f"[BusySchedule] Repairing invalid schedule format "
                    f"({repair_attempt + 1}/{self._FORMAT_REPAIR_ATTEMPTS}): {reason}"
                )
                repair_prompt = (
                    "你之前的 JSON 未通过日程协议校验，请修复后重写。\n"
                    f"校验原因：{reason}\n\n"
                    "只输出 JSON 对象本体，不要 Markdown 或解释。\n"
                    "保留原有明确计划、穿搭和活动内容，只修复格式。\n"
                    "JSON 必须包含 outfit_style、outfit、schedule；hairstyle 可选。\n"
                    "schedule 的每个非空行只能是以下两种格式之一：\n"
                    "1. 普通活动：HH:MM-HH:MM 活动描述 【忙碌/可回消息】；"
                    "必须同时保留开始和结束时间\n"
                    "2. 最后一行睡觉：HH:MM 睡觉 【忙碌】；"
                    "只有这一行可以省略结束时间\n"
                    "如果普通活动缺少结束时间，必须为它补上合理的结束时间，"
                    "不要通过修改【忙碌/可回消息】来规避错误。\n"
                    "保留原有活动内容和明确计划，只调整不符合协议的格式。\n"
                    "删除所有说明、标题、注释，包括任何“未标注时段”说明行。\n\n"
                    f"待修复输出：\n{content[:4000]}"
                )

                sid = (
                    f"busy_schedule_gen_{target_date.strftime('%Y%m%d')}"
                    f"_repair_{repair_attempt + 1}"
                )
                content = await self._call_llm(repair_prompt, provider, sid)
                result = _extract_json_obj(content)

            if not result or not result.get("schedule") or validation_error:
                reason = validation_error or "缺少有效 schedule"
                raise DeterministicScheduleError(
                    "Schedule format remained invalid after "
                    f"{self._FORMAT_REPAIR_ATTEMPTS} repair attempt(s): {reason}"
                )

            # Commit only after the complete result has passed validation.
            data = ScheduleData(
                date=target_date.strftime("%Y-%m-%d"),
                outfit_style=result.get("outfit_style", ""),
                outfit=result.get("outfit", ""),
                hairstyle=result.get("hairstyle", ""),
                schedule=result.get("schedule", ""),
                weather=weather,
                status="completed",
            )
            data.busy_periods = periods
            self.data_mgr.set(target_date, data)

            logger.info(
                f"[BusySchedule] Schedule generated with {len(data.busy_periods)} busy periods"
            )

            if not self._generation_future.done():
                self._generation_future.set_result(data)

            return data

        except Exception as e:
            logger.error(f"[BusySchedule] Schedule generation failed: {e}")
            if self._generation_future and not self._generation_future.done():
                self._generation_future.set_exception(e)
                self._generation_future.exception()
            raise
        finally:
            self._generating = False
            self._generation_target = None
            self._generation_future = None
