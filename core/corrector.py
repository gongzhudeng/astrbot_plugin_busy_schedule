"""Background schedule correction for the current cycle.

At configured times each day the corrector re-reads live context (real-time
mood, attention items, fresh memories) and decides whether the remaining
schedule needs small atomic adjustments.  It reuses the exact operation set
and validation of :class:`~core.schedule_editor.ScheduleEditor`, so an
invalid or unsafe operation is skipped instead of corrupting the day.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

from astrbot.api import logger

from .calendar_context import build_calendar_context
from .data import (
    ResolvedPeriod,
    ScheduleDataManager,
    parse_clock_time,
    parse_schedule_time,
    resolve_schedule_periods,
)
from .generator import _SCHEMA_DEFAULTS, ScheduleGenerator, _extract_json_obj
from .schedule_editor import (
    ScheduleEditConflict,
    ScheduleEditError,
    ScheduleEditNeedsConfirmation,
    ScheduleEditor,
)

_MAX_OPERATIONS_PER_RUN = 12

_CORRECTION_RULES_SUFFIX = (
    "\n\n## 修正原则（必须遵循）\n"
    "1. 默认不动：如果没有明确理由，输出 changed=false，不要为了改而改。\n"
    "2. 需要修正的信号：待关注事项里有用户今天提出的约定或要求；"
    "心情与当前活动明显冲突；用户在聊天里明确要求改穿搭或改计划。\n"
    "3. 只处理今天的事：待关注事项中只有今天内、有明确时间、明确承诺、明确行为的条目"
    "才纳入修正；长期愿望（如“以后多拍视频给我”）、明天或更晚才执行的事一律不动；"
    "事项里写的具体日期若是今天（无论写“今天”还是“10月1号”这类日期写法），都按今天对待。\n"
    "4. operations 里每个对象是一次原子操作："
    "add（start_time、end_time、activity、is_busy）；"
    "update/remove（target_start_time 定位，可选 target_activity，update 可改 "
    "start_time、end_time、activity、is_busy）；"
    "set_outfit（outfit，可选 outfit_style、hairstyle，hairstyle 传空字符串表示去掉发型）。\n"
    "5. set_outfit 仅在用户明确要求改穿搭时使用：如果只是小怡自己因为活动需要"
    "（如要出门、要运动）而临时调整穿着，不要修改今日穿搭，"
    "直接把穿着说明写进对应活动的描述里（如“小怡换上外出服后出门”）。\n"
    "6. 过去的活动不可修改；当前进行中的活动只能改 end_time；"
    "新增活动必须在未来且不与现有活动重叠，必要时先 update 挪开冲突；"
    "最后一条睡觉活动不可删除、不可改成普通活动。\n"
    "7. 分类标签照原格式写在活动描述末尾、状态标记之前，固定顺序"
    "【外出】→【用餐】→【主动分享】；涉及降雨的活动要带上天气提醒。\n"
    "8. 只输出一个 JSON 对象，不要输出任何解释文字：\n"
    '{{"changed": true, "reason": "一句话中文说明", '
    '"operations": [{{"action": "add", "start_time": "15:00", '
    '"end_time": "16:00", "activity": "小怡在天台拍照", "is_busy": false}}]}}\n'
    "changed=false 时 operations 传空数组。"
)

_DEFAULT_TEMPLATE = (
    "# Role: Schedule Corrector\n"
    "你正在修正今天剩余的日程。这不是重新生成一整天，"
    "而是判断当前安排是否需要小幅调整。\n\n"
    "## Context\n"
    "- 日期：{date_str} {weekday}\n"
    "- 当前时间：{current_time}\n"
    "- 今日天气（余下时段）：{weather_forecast}\n"
    "- 当前穿搭：{outfit}{hairstyle_line}\n\n"
    "## 今日剩余日程（标注了正在进行的活动）\n"
    "{remaining_schedule}\n\n"
    "## 当前内心状态（实时）\n"
    "{emotion_context}\n\n"
    "## 待关注事项（用户在聊天中提过的约定与要求）\n"
    "{attention_context}\n\n"
    "## 近期聊天记录（兜底参考：防关注事项漏记、防记忆尚未沉淀）\n"
    "{recent_chats}\n\n"
    "## 近期新记忆（上次日程节点以来）\n"
    "{memory_context}\n" + _CORRECTION_RULES_SUFFIX
)


@dataclass
class CorrectionOutcome:
    """Result of one correction run, safe to show to an admin."""

    triggered: bool = False
    changed: bool = False
    applied: int = 0
    skipped: int = 0
    reason: str = ""
    changes: tuple[str, ...] = field(default_factory=tuple)
    note: str = ""

    def summary(self) -> str:
        if not self.triggered:
            return f"未触发修正：{self.note or self.reason or '条件不满足'}"
        if not self.changed:
            detail = self.reason or "无需调整"
            extra = f"；{self.note}" if self.note else ""
            return f"修正完成，日程未变：{detail}{extra}"
        lines = [
            f"修正完成：应用 {self.applied} 项操作"
            + (f"，跳过 {self.skipped} 项" if self.skipped else ""),
        ]
        if self.reason:
            lines.append(f"原因：{self.reason}")
        if self.changes:
            shown = "\n".join(f"- {item}" for item in self.changes[:8])
            lines.append(f"变更明细：\n{shown}")
        if self.note:
            lines.append(self.note)
        return "\n".join(lines)


def parse_correction_times(value: Any) -> list[str]:
    """Normalize the configured correction time list into sorted HH:MM strings."""
    raw: list[Any] = []
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, list):
        raw = value
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            hour, minute = parse_clock_time(text)
        except ValueError:
            logger.warning(f"[BusySchedule] Invalid correction time {text!r}; skipped")
            continue
        normalized = f"{hour:02d}:{minute:02d}"
        if normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return sorted(out)


class ScheduleCorrector:
    """One-shot corrector reusing the generator's LLM plumbing and editor."""

    def __init__(
        self,
        context: Any,
        config: Any,
        data_mgr: ScheduleDataManager,
        generator: ScheduleGenerator,
        *,
        after_apply: Callable[[], Awaitable[None]] | None = None,
    ):
        self.context = context
        self.config = config
        self.data_mgr = data_mgr
        self.generator = generator
        self._after_apply = after_apply

    # ------------------------------------------------------------------
    # config helpers
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default: Any = None) -> Any:
        group = self.config.get("日程修正", {})
        if isinstance(group, dict) and key in group:
            val = group[key]
            if val is not None and val != "" and val != {} and val != []:
                return val
        value = self.config.get(key)
        if value is not None and value != "" and value != {} and value != []:
            return value
        schema_default = _SCHEMA_DEFAULTS.get(key)
        if schema_default is not None:
            return schema_default
        return default

    def _attention_limit(self) -> int:
        try:
            return max(0, min(10, int(self._cfg("correction_attention_max_items", 5))))
        except (TypeError, ValueError):
            return 5

    def _memory_limits(self) -> tuple[int, int]:
        try:
            items = max(0, min(10, int(self._cfg("correction_memory_max_items", 2))))
        except (TypeError, ValueError):
            items = 2
        try:
            chars = max(
                100, min(4000, int(self._cfg("correction_memory_max_chars", 400)))
            )
        except (TypeError, ValueError):
            chars = 400
        return items, chars

    def _retry_and_timeout(self) -> tuple[int, float]:
        try:
            retries = max(0, min(10, int(self._cfg("correction_model_max_retries", 1))))
        except (TypeError, ValueError):
            retries = 1
        try:
            timeout = max(
                1.0, min(3600.0, float(self._cfg("correction_model_timeout_seconds", 60)))
            )
        except (TypeError, ValueError):
            timeout = 60.0
        return retries, timeout

    # ------------------------------------------------------------------
    # context builders
    # ------------------------------------------------------------------
    def _resolved_timeline(
        self, owner_date: date, schedule_time: tuple[int, int]
    ) -> list[ResolvedPeriod]:
        active = self.data_mgr.get_active(owner_date)
        if active is None:
            return []
        next_active = self.data_mgr.get_active(owner_date + timedelta(days=1))
        try:
            return resolve_schedule_periods(active, schedule_time, next_active)
        except ValueError as exc:
            logger.warning(
                f"[BusySchedule] Correction could not resolve timeline: {exc}"
            )
            return []

    @staticmethod
    def _render_remaining(
        resolved: list[ResolvedPeriod],
        fallback_text: str,
        now: datetime,
    ) -> str:
        if not resolved:
            return fallback_text or "（今日已无剩余活动）"
        lines: list[str] = []
        for item in sorted(resolved, key=lambda entry: entry.start):
            if item.end <= now:
                continue
            period = item.period
            marker = "忙碌" if period.is_busy else "可回消息"
            if period.is_open_sleep:
                lines.append(f"{period.start_time} {period.activity}【{marker}】")
                continue
            suffix = "（当前正在进行）" if item.contains(now) else ""
            lines.append(
                f"{period.start_time}-{period.end_time} "
                f"{period.activity}【{marker}】{suffix}"
            )
        return "\n".join(lines) or "（今日已无剩余活动）"

    def _recent_chat_rounds(self) -> int:
        try:
            return max(0, min(30, int(self._cfg("correction_recent_chat_rounds", 6))))
        except (TypeError, ValueError):
            return 6

    async def _build_context(
        self,
        owner_date: date,
        schedule_time: tuple[int, int],
        umo: str | None,
        memory_since: str,
        now: datetime,
    ) -> tuple[str, dict[str, Any]]:
        active = self.data_mgr.get_active(owner_date)
        assert active is not None  # run_correction guarantees this
        data = active.data

        calendar = build_calendar_context(now.date(), self.config)
        remaining = self._render_remaining(
            self._resolved_timeline(owner_date, schedule_time),
            data.schedule,
            now,
        )

        mood_text = "暂无实时心情数据。"
        attention_text = "暂无待关注事项。"
        live_callback = getattr(self.context, "_emotion_state_get_live_context", None)
        if callable(live_callback) and umo:
            try:
                live = await live_callback(umo, self._attention_limit())
                if isinstance(live, dict):
                    mood_text = (
                        str(live.get("mood") or "").strip() or mood_text
                    )
                    attention_text = (
                        str(live.get("attention") or "").strip() or attention_text
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[BusySchedule] Live emotion context unavailable: {exc}")

        memory_text = "暂无近期新记忆。"
        memory_callback = getattr(
            self.context, "_livingmemory_get_recent_memories", None
        )
        if callable(memory_callback) and umo and memory_since:
            items_limit, chars_limit = self._memory_limits()
            if items_limit > 0:
                try:
                    items = await memory_callback(umo, str(memory_since), items_limit)
                    lines = [
                        f"- [{str(item.get('time') or '').strip()}] "
                        f"{str(item.get('text') or '').strip()}"
                        for item in (items or [])
                        if isinstance(item, dict) and str(item.get("text") or "").strip()
                    ]
                    if lines:
                        memory_text = "\n".join(lines)[:chars_limit]
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"[BusySchedule] Recent memories unavailable: {exc}"
                    )

        weather_text = "天气预报暂不可用。请按其他上下文正常规划，不要虚构具体天气。"
        weather_service = getattr(self.generator, "weather_service", None)
        if weather_service:
            try:
                weather = await weather_service.get_forecast(owner_date, schedule_time)
                if weather is not None:
                    weather_text = weather.format_for_prompt()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[BusySchedule] Weather unavailable for correction: {exc}")

        chats_text = "无近期对话记录。"
        recent_rounds = self._recent_chat_rounds()
        if recent_rounds > 0 and umo:
            try:
                chats_text = (
                    await self.generator._get_recent_chats(umo, recent_rounds)
                    or "无近期对话记录。"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[BusySchedule] Recent chats unavailable: {exc}")

        ctx: dict[str, Any] = {
            **calendar,
            "current_time": now.strftime("%H:%M"),
            "outfit": data.outfit or "未设置",
            "hairstyle_line": f"\n发型：{data.hairstyle}" if data.hairstyle else "",
            "weather_forecast": weather_text,
            "remaining_schedule": remaining,
            "emotion_context": mood_text,
            "attention_context": attention_text,
            "recent_chats": chats_text,
            "memory_context": memory_text,
        }

        template = str(self._cfg("correction_prompt_template", "") or "").strip()
        if not template:
            template = _DEFAULT_TEMPLATE
        try:
            prompt = template.format(**ctx)
        except (KeyError, IndexError) as exc:  # noqa: BLE001
            logger.warning(
                f"[BusySchedule] correction_prompt_template has unknown "
                f"placeholder: {exc}"
            )
            prompt = template
            for key, value in ctx.items():
                prompt = prompt.replace(f"{{{key}}}", str(value))
        return prompt, ctx

    # ------------------------------------------------------------------
    # main entry
    # ------------------------------------------------------------------
    async def run_correction(
        self,
        owner_date: date,
        umo: str | None,
        *,
        memory_since: str = "",
        now: datetime | None = None,
    ) -> CorrectionOutcome:
        now = now or datetime.now()
        schedule_time = parse_schedule_time(self._cfg("schedule_time", "07:00"))
        active = self.data_mgr.get_active(owner_date)
        if active is None or active.data.status != "completed":
            return CorrectionOutcome(
                triggered=False, note="当前周期没有已完成的日程"
            )

        try:
            prompt, _ctx = await self._build_context(
                owner_date, schedule_time, umo, memory_since, now
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[BusySchedule] Correction prompt build failed: {exc}")
            return CorrectionOutcome(triggered=True, note=f"上下文构建失败：{exc}")

        primary_override = str(self._cfg("correction_llm_provider", "") or "").strip()
        providers = self.generator._get_providers(primary_override=primary_override or None)
        retries, timeout = self._retry_and_timeout()
        session_id = f"busy_schedule_correction_{uuid.uuid4().hex[:8]}"
        try:
            text, _provider = await self.generator._call_llm(
                prompt,
                providers,
                session_id,
                max_retries=retries,
                timeout_seconds=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[BusySchedule] Correction LLM call failed: {exc}")
            return CorrectionOutcome(triggered=True, note=f"模型调用失败：{exc}")

        try:
            payload = _extract_json_obj(text)
            if not isinstance(payload, dict):
                raise ValueError("correction response is not a JSON object")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[BusySchedule] Correction response parse failed: {exc}")
            return CorrectionOutcome(triggered=True, note=f"模型输出解析失败：{exc}")

        changed = bool(payload.get("changed", False))
        reason = str(payload.get("reason", "") or "").strip()[:300]
        raw_operations = payload.get("operations")
        if not changed or not isinstance(raw_operations, list):
            logger.info(
                f"[BusySchedule] Correction decided no change: {reason or 'unspecified'}"
            )
            return CorrectionOutcome(triggered=True, changed=False, reason=reason)

        operations = [op for op in raw_operations if isinstance(op, dict)][
            :_MAX_OPERATIONS_PER_RUN
        ]
        if not operations:
            return CorrectionOutcome(triggered=True, changed=False, reason=reason)

        editor = ScheduleEditor()
        current = active.data
        applied_changes: list[str] = []
        applied = 0
        skipped = 0
        for operation in operations:
            try:
                result = editor.apply(
                    current,
                    [operation],
                    owner_date=owner_date,
                    schedule_time=schedule_time,
                    now=now,
                    confirmed_important=False,
                )
            except ScheduleEditNeedsConfirmation as exc:
                skipped += 1
                logger.info(
                    f"[BusySchedule] Correction skipped op needing confirmation: {exc}"
                )
                continue
            except (ScheduleEditConflict, ScheduleEditError, ValueError, TypeError) as exc:
                skipped += 1
                logger.warning(f"[BusySchedule] Correction skipped invalid op: {exc}")
                continue
            current = result.data
            applied_changes.extend(result.changes)
            applied += 1

        if applied == 0:
            logger.warning(
                "[BusySchedule] Correction produced no applicable operation; "
                "schedule unchanged"
            )
            return CorrectionOutcome(
                triggered=True,
                changed=False,
                skipped=skipped,
                reason=reason,
                note="所有操作都被校验拒绝，日程保持原样",
            )

        self.data_mgr.set(owner_date, current)
        if self._after_apply is not None:
            try:
                await self._after_apply()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[BusySchedule] Post-correction refresh failed: {exc}")

        logger.info(
            f"[BusySchedule] Correction applied {applied} op(s), skipped {skipped}: "
            f"{reason or 'unspecified'}"
        )
        return CorrectionOutcome(
            triggered=True,
            changed=True,
            applied=applied,
            skipped=skipped,
            reason=reason,
            changes=tuple(applied_changes),
        )
