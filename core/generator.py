"""Schedule generator module - generates daily schedule with busy period markers."""

import asyncio
import json
import random
import re
import uuid
from datetime import datetime, date, timedelta
from typing import Optional

from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context

from .data import ScheduleData, ScheduleDataManager, BusyPeriod


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


class ScheduleGenerator:
    """Generates daily schedule with busy period markers."""

    _MAX_RETRIES = 3

    def __init__(self, context: Context, config: AstrBotConfig, data_mgr: ScheduleDataManager):
        self.context = context
        self.config = config
        self.data_mgr = data_mgr
        self._generating = False
        self._generation_future: Optional[asyncio.Future] = None

    def _cfg(self, key: str, default=None):
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

    def _get_judge_provider(self):
        """Get LLM provider for smart judgment."""
        provider_id = self._cfg("llm_provider_judge", "")
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider:
                return provider
        return self.context.get_using_provider()

    async def _get_judge_persona(self, umo: Optional[str] = None) -> str:
        """Get persona system_prompt for smart judgment.

        Priority: judge_persona_id config > current conversation persona > empty.
        """
        try:
            persona_id = self._cfg("judge_persona_id", "")

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

            if persona_id:
                persona_mgr = self.context.persona_manager
                if persona_mgr:
                    for persona in persona_mgr.personas:
                        if persona.persona_id == persona_id and persona.system_prompt:
                            return persona.system_prompt
        except Exception as e:
            logger.warning(f"[BusySchedule] Failed to resolve judge persona: {e}")
        return ""

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
                            return persona.system_prompt[:500] if persona.system_prompt else ""

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
        except Exception as e:
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
                history.append(f"[{past_date.strftime('%Y-%m-%d')}] 风格：{style} 穿搭：{outfit} 日程：{schedule}")
            else:
                history.append(f"[{past_date.strftime('%Y-%m-%d')}] 穿搭：{outfit} 日程：{schedule}")
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

        lines = [line.strip() for line in data.schedule.strip().split("\n") if line.strip()]
        if not lines:
            return ""

        last_line = lines[-1]
        logger.info(f"[BusySchedule] Yesterday's last activity: {last_line}")
        return last_line

    async def _get_recent_chats(self, umo: Optional[str] = None, count: int = 0) -> str:
        """Get recent chat messages for reference via conversation_manager."""
        count = count or self._cfg("reference_recent_count", 10)
        if not umo or not count:
            return "无近期对话"

        try:
            conv_mgr = self.context.conversation_manager
            if not conv_mgr:
                return "无近期对话"

            cid = await conv_mgr.get_curr_conversation_id(umo)
            if not cid:
                return "无近期对话记录"

            conv = await conv_mgr.get_conversation(umo, cid)
            if not conv or not getattr(conv, "history", None):
                return "无近期对话记录"

            history = json.loads(conv.history)
            recent = history[-count:] if count > 0 else history

            formatted = []
            for msg in recent:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if not content:
                    continue
                # Truncate very long messages
                if len(content) > 200:
                    content = content[:200] + "..."
                if role == "user":
                    formatted.append(f"用户: {content}")
                elif role == "assistant":
                    formatted.append(f"我: {content}")

            return "\n".join(formatted) if formatted else "无近期对话记录"
        except Exception as e:
            return "无近期对话记录"

    async def _get_conversation_contexts(self, umo: str, rounds: int) -> list:
        """Fetch the last N rounds of conversation history as context dicts.

        Returns a list of {"role": "user"/"assistant", "content": "..."} dicts,
        suitable for passing to provider.text_chat(contexts=...).
        Each round = 1 user message + 1 assistant message.
        """
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

            history = json.loads(conversation.history) if isinstance(conversation.history, str) else conversation.history
            if not isinstance(history, list):
                return []

            # Collect messages in reverse, then take last N rounds
            msgs = []
            for msg in reversed(history):
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                if not content:
                    continue
                msgs.append({"role": role, "content": str(content)})
                if len(msgs) >= rounds * 2:
                    break

            msgs.reverse()
            return msgs
        except Exception as e:
            logger.warning(f"[BusySchedule] Failed to get conversation contexts: {e}")
            return []

    async def _build_prompt(self, target_date: date, extra: Optional[str] = None, umo: Optional[str] = None) -> str:
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
            "schedule_type": random.choice(schedule_types) if schedule_types else "随性漫游型",
            "history_schedules": self._get_history_schedules(target_date),
            "last_yesterday_activity": self._get_yesterday_last_activity(target_date),
            "recent_chats": await self._get_recent_chats(umo),
        }

        try:
            prompt = template.format(**ctx)
        except KeyError as e:
            logger.warning(f"[BusySchedule] prompt_template has unknown placeholder: {e}")
            # Fallback: fill what we can
            prompt = template
            for k, v in ctx.items():
                prompt = prompt.replace(f"{{{k}}}", str(v))

        if extra:
            prompt += f"\n\n## 用户补充要求\n{extra}"

        return prompt

    def _parse_busy_periods_from_schedule(self, schedule_text: str) -> list[BusyPeriod]:
        """Parse busy periods from schedule text with markers."""
        periods = []
        pattern = r"(\d{1,2}:\d{2})\s*[-~到至]\s*(\d{1,2}:\d{2})\s+(.+?)(?:\s*【(忙碌|可回消息)】)?(?:\n|$)"

        for match in re.finditer(pattern, schedule_text):
            start_time = match.group(1)
            end_time = match.group(2)
            activity = match.group(3).strip()
            marker = match.group(4)

            is_busy = True
            if marker == "可回消息":
                is_busy = False
            elif marker is None:
                non_busy_keywords = ["刷手机", "休息", "散步", "闲逛", "发呆", "赖床", "看剧", "玩"]
                is_busy = not any(kw in activity for kw in non_busy_keywords)

            periods.append(BusyPeriod(
                start_time=start_time,
                end_time=end_time,
                activity=activity,
                is_busy=is_busy,
            ))

        return periods

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

    async def _call_llm(self, prompt: str, provider, session_id: str, system_prompt: str = "") -> str:
        """Call LLM and return completion text. Retries on empty response."""
        for attempt in range(self._MAX_RETRIES):
            try:
                resp = await provider.text_chat(prompt=prompt, session_id=session_id, system_prompt=system_prompt or None)
                text = _extract_completion_text(resp)
                if text:
                    return text
                logger.warning(f"[BusySchedule] Empty LLM response (attempt {attempt + 1}/{self._MAX_RETRIES})")
            except Exception as e:
                logger.warning(f"[BusySchedule] LLM call failed (attempt {attempt + 1}): {e}")
            finally:
                await self._cleanup_session(session_id)
                # Use a new session_id for retry
                session_id = f"busy_schedule_{uuid.uuid4().hex[:8]}"

        raise RuntimeError("LLM returned empty response after all retries")

    async def generate_schedule_or_wait(
        self, target_date: date, umo: Optional[str] = None, extra: Optional[str] = None
    ) -> ScheduleData:
        """Generate schedule, or wait if one is already in progress."""
        if self._generating and self._generation_future:
            logger.info("[BusySchedule] Waiting for in-progress generation...")
            try:
                result = await asyncio.wait_for(self._generation_future, timeout=120)
                return result
            except asyncio.TimeoutError:
                logger.warning("[BusySchedule] Wait for generation timed out, retrying")
            except Exception:
                pass
        return await self.generate_schedule(target_date, umo, extra)

    async def generate_schedule(
        self, target_date: date, umo: Optional[str] = None, extra: Optional[str] = None
    ) -> ScheduleData:
        """Generate schedule for a specific date."""
        if self._generating:
            logger.warning("[BusySchedule] Schedule generation already in progress")
            raise RuntimeError("Schedule generation already in progress")

        self._generating = True
        self._generation_future = asyncio.get_event_loop().create_future()
        data = None
        try:
            data = self.data_mgr.get_or_create(target_date)
            data.status = "generating"
            self.data_mgr.set(target_date, data)

            prompt = await self._build_prompt(target_date, extra, umo)
            provider = self._get_provider()

            if not provider:
                raise RuntimeError("No LLM provider available")

            logger.info(f"[BusySchedule] Generating schedule for {target_date}")

            # Call LLM with retry and JSON extraction
            sid = f"busy_schedule_gen_{target_date.strftime('%Y%m%d')}_0"
            content = await self._call_llm(prompt, provider, sid)

            result = _extract_json_obj(content)

            # Retry with repair prompt if JSON is invalid or missing fields
            for attempt in range(1, self._MAX_RETRIES + 1):
                if result and result.get("schedule"):
                    break

                reason = "未能解析出 JSON 对象" if not result else "schedule 字段为空"
                repair_prompt = (
                    f"你之前的输出未通过校验，需要重写。\n"
                    f"校验原因：{reason}\n\n"
                    f"请只输出 JSON 对象本体，不要 Markdown，不要解释。\n"
                    f"输出 JSON 必须包含字段：outfit_style、outfit、schedule。\n\n"
                    f"你之前的输出（供参考）：\n{content[:500]}\n\n"
                    f"原始任务：\n{prompt}"
                )

                sid = f"busy_schedule_gen_{target_date.strftime('%Y%m%d')}_{attempt}"
                content = await self._call_llm(repair_prompt, provider, sid)
                result = _extract_json_obj(content)

            if not result or not result.get("schedule"):
                raise RuntimeError(f"Failed to get valid schedule after {self._MAX_RETRIES} attempts")

            # Update data
            data.outfit_style = result.get("outfit_style", "")
            data.outfit = result.get("outfit", "")
            data.schedule = result.get("schedule", "")
            data.busy_periods = self._parse_busy_periods_from_schedule(data.schedule)
            data.status = "completed"
            self.data_mgr.set(target_date, data)

            logger.info(f"[BusySchedule] Schedule generated with {len(data.busy_periods)} busy periods")

            if not self._generation_future.done():
                self._generation_future.set_result(data)

            return data

        except Exception as e:
            logger.error(f"[BusySchedule] Schedule generation failed: {e}")
            if data:
                data.status = "failed"
                self.data_mgr.set(target_date, data)
            if self._generation_future and not self._generation_future.done():
                self._generation_future.set_exception(e)
            raise
        finally:
            self._generating = False
            self._generation_future = None

    async def adjust_schedule(
        self, target_date: date, recent_chats: str, current_time: datetime
    ) -> Optional[list[BusyPeriod]]:
        """Dynamically adjust busy periods based on chat context."""
        if not self._cfg("enable_dynamic_adjust", False):
            return None

        data = self.data_mgr.get(target_date)
        if not data or data.status != "completed":
            return None

        provider = self._get_provider()
        if not provider:
            return None

        adjust_prompt = self._cfg("adjust_prompt", "")
        prompt = adjust_prompt.replace("{today_schedule}", data.schedule)
        prompt = prompt.replace("{recent_chats}", recent_chats)
        prompt = prompt.replace("{current_time}", current_time.strftime("%H:%M"))

        sid = f"busy_schedule_adjust_{target_date.strftime('%Y%m%d')}"
        try:
            content = await self._call_llm(prompt, provider, sid)
            result = _extract_json_obj(content)

            if not result or not result.get("adjust", False):
                return None

            new_periods = []
            for period in result.get("new_periods", []):
                new_periods.append(BusyPeriod(
                    start_time=period["start"],
                    end_time=period["end"],
                    activity=period["activity"],
                    is_busy=True,
                ))

            if new_periods:
                self.data_mgr.update_busy_periods(target_date, new_periods)
                logger.info(f"[BusySchedule] Schedule adjusted with {len(new_periods)} new periods")
                return new_periods

        except Exception as e:
            logger.error(f"[BusySchedule] Schedule adjustment failed: {e}")

        return None

    async def judge_and_adjust_schedule(
        self, target_date: date, user_message: str, umo: Optional[str] = None
    ) -> Optional[dict]:
        """Use LLM to judge if user wants to adjust schedule."""
        if not self._cfg("enable_smart_judge", False):
            logger.warning("[BusySchedule] judge_and_adjust_schedule: enable_smart_judge is False")
            return None

        data = self.data_mgr.get(target_date)
        if not data or data.status != "completed":
            logger.warning(f"[BusySchedule] judge_and_adjust_schedule: no data or status={data.status if data else 'None'}")
            return None

        provider = self._get_judge_provider()
        if not provider:
            logger.warning("[BusySchedule] judge_and_adjust_schedule: no LLM provider available")
            return None

        now = datetime.now()

        # Get conversation context for better judgment
        context_str = ""
        if umo:
            history_rounds = self._cfg("judge_history_rounds", 3)
            if history_rounds > 0:
                contexts = await self._get_conversation_contexts(umo, history_rounds)
                if contexts:
                    context_lines = []
                    for ctx in contexts:
                        role = "用户" if ctx["role"] == "user" else "AI"
                        content = ctx["content"][:150]  # Truncate long messages
                        context_lines.append(f"{role}: {content}")
                    context_str = "\n".join(context_lines)
                    logger.info(f"[BusySchedule] Judge context: {len(contexts)} messages, preview={context_str[:300]}")
                else:
                    logger.warning("[BusySchedule] Judge context: conversation contexts returned empty")

        # Build judge prompt
        judge_prompt = self._cfg("judge_prompt", "")
        prompt = judge_prompt.replace("{today_schedule}", data.schedule)
        prompt = prompt.replace("{current_time}", now.strftime("%H:%M"))
        prompt = prompt.replace("{user_message}", user_message)
        prompt = prompt.replace("{conversation_context}", context_str or "无对话上下文")

        sid = f"busy_schedule_judge_{target_date.strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"
        try:
            judge_persona = await self._get_judge_persona(umo)
            logger.info(f"[BusySchedule] Calling LLM for judge, sid={sid}, prompt_len={len(prompt)}, persona={'yes' if judge_persona else 'no'}")
            content = await self._call_llm(prompt, provider, sid, system_prompt=judge_persona)
            logger.info(f"[BusySchedule] LLM response len={len(content) if content else 0}, preview={content[:200] if content else 'EMPTY'}")
            result = _extract_json_obj(content)

            if not result:
                logger.warning(f"[BusySchedule] Failed to extract JSON from LLM response")
                return None

            if not result.get("should_adjust", False):
                return {"adjusted": False, "reason": result.get("reason", "")}

            adjustments = result.get("adjustments", [])
            if not adjustments:
                return {"adjusted": False, "reason": result.get("reason", "")}

            # Apply adjustments
            new_schedule = data.schedule
            new_busy_periods = list(data.busy_periods)

            for adj in adjustments:
                original_activity = adj.get("original_activity", "")
                original_time = adj.get("original_time", "")
                new_activity = adj.get("new_activity", original_activity)
                new_time = adj.get("new_time", original_time)
                is_busy = adj.get("is_busy", False)

                if original_activity and original_time:
                    old_pattern = f"{original_time}\\s+.*?{re.escape(original_activity)}"
                    new_text = f"{new_time} {new_activity} {'【忙碌】' if is_busy else '【可回消息】'}"
                    new_schedule = re.sub(old_pattern, new_text, new_schedule)

                for i, period in enumerate(new_busy_periods):
                    if period.activity == original_activity:
                        new_busy_periods[i] = BusyPeriod(
                            start_time=new_time.split("-")[0] if "-" in new_time else period.start_time,
                            end_time=new_time.split("-")[1] if "-" in new_time else period.end_time,
                            activity=new_activity,
                            is_busy=is_busy,
                        )

            data.schedule = new_schedule
            data.busy_periods = new_busy_periods
            data.last_updated = now.isoformat()
            self.data_mgr.set(target_date, data)

            logger.info(f"[BusySchedule] Schedule adjusted by smart judge: {adjustments}")

            return {
                "adjusted": True,
                "reason": result.get("reason", ""),
                "adjustments": adjustments,
            }

        except Exception as e:
            logger.error(f"[BusySchedule] Smart judge failed: {e}")
            return None