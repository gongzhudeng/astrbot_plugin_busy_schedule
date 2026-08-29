"""Glassmorphism Pillow renderer for busy schedule command images (B7)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .style_kit import Canvas, c, font


@dataclass(frozen=True)
class BusyStatusImageData:
    """Values displayed by the busy status command image."""

    is_busy: bool
    activity: str
    remaining_minutes: int | None
    current_start: str
    current_end: str
    next_busy: str
    chat_protection: str
    queued_messages: int
    queued_users: int


class BusyScheduleImageRenderer:
    """Render schedule and status images without browser dependencies."""

    width = 1080
    day_start_hour = 7
    night_start_hour = 19

    def __init__(self, plugin_dir: Path):
        """Initialize renderer paths.

        Args:
            plugin_dir: Plugin root containing the replaceable ``logo.png``.
        """
        self.plugin_dir = Path(plugin_dir)

    def resolve_theme(self, mode: object, now: datetime) -> str:
        """Resolve a configured display mode to a concrete theme.

        Args:
            mode: Configured Chinese or English theme mode.
            now: Current time in the AstrBot configured timezone.

        Returns:
            ``day`` or ``night``.
        """
        normalized = str(mode or "").strip().casefold()
        if normalized in {"白天模式", "day", "light"}:
            return "day"
        if normalized in {"夜间模式", "night", "dark"}:
            return "night"
        return (
            "day"
            if self.day_start_hour <= now.hour < self.night_start_hour
            else "night"
        )

    def render_schedule(
        self,
        data: Any,
        now: datetime,
        is_busy: bool,
        mode: object = "自动切换",
        source_note: str = "",
        calendar_context: dict[str, str] | None = None,
    ) -> bytes:
        """Render one complete schedule as PNG bytes.

        Args:
            data: ScheduleData-like object with outfit, weather, and schedule.
            now: Current local time used for highlighting and theme selection.
            is_busy: Current plugin busy state.
            mode: Automatic, day, or night display mode.
            source_note: Optional warning for fallback schedule data.
            calendar_context: Optional holiday and custom special-day labels.

        Returns:
            Encoded RGB PNG bytes.
        """
        theme = self.resolve_theme(mode, now)
        local_now = now.replace(tzinfo=None)
        entries = self._parse_schedule(
            str(getattr(data, "schedule", "")),
            date.fromisoformat(str(getattr(data, "date"))),
            local_now,
        )
        return self._render_schedule(
            data, local_now, is_busy, entries, source_note, calendar_context, theme
        )

    def render_status(
        self,
        status: BusyStatusImageData,
        now: datetime,
        mode: object = "自动切换",
    ) -> bytes:
        """Render the compact busy status image as PNG bytes.

        Args:
            status: Normalized status values from the command handler.
            now: Current time in the AstrBot configured timezone.
            mode: Automatic, day, or night display mode.

        Returns:
            Encoded RGB PNG bytes.
        """
        theme = self.resolve_theme(mode, now)
        return self._render_status(status, now, theme)

    # -- palette -----------------------------------------------------------
    @staticmethod
    def _palette(theme: str) -> dict[str, Any]:
        if theme == "night":
            return {
                "stops": [(0, c("#171331")), (0.5, c("#1E1A44")), (1, c("#12101F"))],
                "glows": [
                    ("#6A55C8", 880, 260, 300, 85),
                    ("#A85A7A", 140, 1300, 300, 50),
                    ("#4E4E9E", 900, 2400, 300, 50),
                ],
                "ink": "#ECE8F8",
                "sub": "#9890B5",
                "accent": "#A18CF0",
                "coral": "#F08A70",
                "gold": "#D8BC80",
                "tint": (46, 42, 82),
                "talpha": 135,
                "line": (255, 255, 255, 46),
                "shadow_a": 95,
            }
        return {
            "stops": [(0, c("#F0E6F8")), (0.5, c("#E4E8FA")), (1, c("#FAE9E8"))],
            "glows": [
                ("#B79BEB", 880, 260, 300, 85),
                ("#E8A8C0", 140, 1300, 300, 55),
                ("#8FA0E8", 900, 2400, 300, 50),
            ],
            "ink": "#2E2840",
            "sub": "#7E7494",
            "accent": "#8B72E0",
            "coral": "#E0785E",
            "gold": "#C09040",
            "tint": (255, 255, 255),
            "talpha": 135,
            "line": (255, 255, 255, 215),
            "shadow_a": 40,
        }

    # -- shared data helpers (kept from the previous renderer) ---------------
    @staticmethod
    def _at_cycle_time(
        owner_date: date, value: str | None, reference: datetime | None
    ) -> datetime | None:
        if value is None:
            return None
        hour, minute = (int(part) for part in value.split(":"))
        result = datetime.combine(owner_date, datetime.min.time()).replace(
            hour=hour, minute=minute
        )
        if reference is not None:
            while result < reference:
                result += timedelta(days=1)
        return result

    def _parse_schedule(
        self, text: str, owner_date: date, now: datetime
    ) -> list[dict[str, Any]]:
        entries = []
        previous_start = None
        pattern = re.compile(r"^(\d{2}:\d{2})(?:-(\d{2}:\d{2}))?\s+(.+)$")
        for raw in (line.strip() for line in text.splitlines() if line.strip()):
            match = pattern.match(raw)
            if not match:
                continue
            start_text, end_text, content = match.groups()
            tags = re.findall(r"【([^】]+)】", content)
            activity = re.sub(r"【[^】]+】", "", content).strip()
            start = self._at_cycle_time(owner_date, start_text, previous_start)
            previous_start = start
            end = self._at_cycle_time(owner_date, end_text, start)
            if end is not None and start is not None and end <= start:
                end += timedelta(days=1)
            entries.append(
                {
                    "index": len(entries) + 1,
                    "start": start_text,
                    "end": end_text or "—",
                    "activity": activity,
                    "busy": "忙碌" in tags,
                    "current": bool(
                        start
                        and (
                            (end is not None and start <= now < end)
                            or (end is None and now >= start)
                        )
                    ),
                }
            )
        return entries

    @staticmethod
    def _outfit_items(data: Any) -> list[tuple[str, str]]:
        """Parse outfit lines into ``(key, value)`` pairs for two columns."""
        pairs: list[tuple[str, str]] = []
        for line in str(getattr(data, "outfit", "")).splitlines():
            line = line.strip()
            if not line:
                continue
            key, sep, value = line.partition("：")
            pairs.append((key if sep else "", value if sep else line))
        hairstyle = str(getattr(data, "hairstyle", "") or "").strip()
        if hairstyle:
            pairs.append(("发型", hairstyle))
        return pairs or [("", "今日穿搭暂未设置")]

    @staticmethod
    def _weather(data: Any) -> tuple[str, str, str]:
        weather = getattr(data, "weather", None)
        if weather is None:
            return "天气暂不可用", "--℃", "暂无天气数据"
        location = str(getattr(weather, "display_location", "天气")).split("，", 1)[0]
        minimum = getattr(weather, "temperature_min_c", None)
        maximum = getattr(weather, "temperature_max_c", None)
        temperature = (
            f"{minimum:g}~{maximum:g}°C"
            if minimum is not None and maximum is not None
            else "--°C"
        )
        note = str(getattr(weather, "summary", "") or "无明显降水时段")
        return location, temperature, note

    @staticmethod
    def _weekday(value: date) -> str:
        return ("一", "二", "三", "四", "五", "六", "日")[value.weekday()]

    @staticmethod
    def _calendar_observance(calendar_context: dict[str, str] | None) -> str:
        if not calendar_context:
            return ""
        labels: list[str] = []
        holiday = str(calendar_context.get("holiday", "")).strip()
        if holiday and holiday != "无已知节日":
            labels.append(holiday)
        custom_text = str(calendar_context.get("special_days", "")).strip()
        if custom_text and custom_text != "无自定义特别日":
            for line in custom_text.splitlines():
                label = line.removeprefix("- ").strip()
                if label and label not in labels:
                    labels.append(label)
        return " / ".join(labels) if labels else "无已知节日"

    # -- drawing helpers ------------------------------------------------------
    def _card(self, cv: Canvas, box, pal, radius=30):
        cv.shadow(box, radius, 22, 12, alpha=pal["shadow_a"])
        cv.glass(
            box,
            radius=radius,
            tint=pal["tint"],
            alpha=pal["talpha"],
            outline=pal["line"],
            owidth=1.5,
        )

    def _ellipsize(self, probe: Canvas, text: str, f, max_width: float) -> str:
        """Trim ``text`` with an ellipsis so it fits ``max_width``."""
        if probe.tlen(text, f) <= max_width:
            return text
        suffix = "…"
        while text and probe.tlen(text + suffix, f) > max_width:
            text = text[:-1]
        return text + suffix

    def _note_lines(
        self, probe: Canvas, text: str, f, max_width: float, max_lines: int = 2
    ) -> list[str]:
        """Compact the precipitation note, wrapping at range boundaries."""
        parts = str(text).replace("主要降水时段 ", "降水 ").split("、")
        lines = [parts[0]] if parts else []
        for part in parts[1:]:
            if lines and probe.tlen(lines[-1] + "、" + part, f) <= max_width:
                lines[-1] += "、" + part
            else:
                lines.append(part)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1].rstrip("、") + "…"
        return [
            self._ellipsize(probe, line, f, max_width)
            if probe.tlen(line, f) > max_width
            else line
            for line in lines
        ]

    def _header(self, cv: Canvas, pal, title: str, date_line: str, en: str):
        self._card(cv, (58, 52, 1022, 236), pal)
        cv.avatar(self.plugin_dir / "logo.png", 926, 144, 104, pal["line"], 3)
        cv.spaced(92, 84, en, font(17, 500), pal["accent"], 5)
        cv.text(92, 114, title, font(40, 800), pal["ink"])
        cv.text(92, 178, date_line, font(20, 450), pal["sub"])

    # -- schedule image ---------------------------------------------------
    def _render_schedule(
        self,
        data: Any,
        now: datetime,
        is_busy: bool,
        entries: list[dict[str, Any]],
        source_note: str,
        calendar_context: dict[str, str] | None,
        theme: str,
    ) -> bytes:
        pal = self._palette(theme)
        ink, sub = pal["ink"], pal["sub"]
        accent, coral, gold = pal["accent"], pal["coral"], pal["gold"]

        probe = Canvas(height=8)
        af = font(26, 500)
        outfit = self._outfit_items(data)
        outfit_rows = (len(outfit) + 1) // 2
        outfit_lines = [probe.wrap(value, font(22, 500), 310) for _, value in outfit]
        outfit_row_hs = [max(54, 30 + len(ls) * 28) for ls in outfit_lines]
        col_heights = [0, 0]
        for i, hh in enumerate(outfit_row_hs):
            col_heights[i // outfit_rows] += hh
        plan_lines = [probe.wrap(e["activity"], af, 555) for e in entries]
        row_hs = [max(84, 48 + len(ls) * 36) for ls in plan_lines]
        location, temperature, weather_note = self._weather(data)
        note_lines = self._note_lines(probe, weather_note, font(15, 450), 270)

        s_top = 264
        banner_h = 0
        banner_top = s_top + 128 + 24
        if source_note:
            banner_lines = probe.wrap(source_note, font(18, 500), 850)
            banner_h = 28 + len(banner_lines) * 28
        outfit_top = s_top + 128 + (banner_h + 24 if source_note else 32)
        oh = 100 + max(col_heights) + 24
        p_top = outfit_top + oh + 32
        p_h = 104 + sum(row_hs) + 20
        yf = p_top + p_h + 40

        cv = Canvas(
            height=int(yf + 46) + 40, bg="#F0E6F8" if theme == "day" else "#171331"
        )
        cv.bg_gradient(pal["stops"])
        for col, gx, gy, gr, ga in pal["glows"]:
            cv.glow(gx, gy, gr, c(col), ga)

        # header
        display_date = date.fromisoformat(str(data.date))
        observance = self._calendar_observance(calendar_context)
        date_text = (
            f"{display_date:%Y.%m.%d} · 星期{self._weekday(display_date)}"
            + (f" · {observance}" if observance else "")
            + f" · {now:%H:%M}"
        )
        self._header(
            cv,
            pal,
            "小怡的今日行程",
            self._ellipsize(probe, date_text, font(20, 450), 620),
            "VIOLET · DAILY SCHEDULE",
        )
        status_text = "忙碌中 · 暂缓回复" if is_busy else "在线 · 可回消息"
        status_col = coral if is_busy else accent
        cv.pill(
            826,
            182,
            status_text,
            font(17, 600),
            c(status_col),
            c(status_col, 34),
            padx=16,
            pady=8,
            anchor="ra",
        )

        # summary strip: dynamic counts + weather
        self._card(cv, (58, s_top, 1022, s_top + 128), pal, radius=26)
        n_busy = sum(1 for e in entries if e["busy"])
        stats = [
            ("时段", str(len(entries)), ""),
            ("忙碌", str(n_busy), " 段"),
            ("空闲", str(len(entries) - n_busy), " 段"),
        ]
        nf, uf = font(34, 700, "num"), font(18, 600)
        base = s_top + 62
        for i, (label, value, unit) in enumerate(stats):
            x = 92 + i * 196
            cv.text(x, base, value, nf, accent if i else ink, anchor="ls")
            if unit:
                cv.text(x + cv.tlen(value, nf) + 4, base, unit, uf, ink, anchor="ls")
            cv.text(x, s_top + 86, label, font(17, 500), sub)
        cv.vline(668, s_top + 24, s_top + 104, c(ink, 40), 1.5)
        cv.text(700, s_top + 26, f"{location} · {temperature}", font(22, 700), ink)
        ny = s_top + 66
        for line in note_lines:
            cv.text(700, ny, line, font(15, 450), sub)
            ny += 22

        # optional fallback-data notice
        if source_note and banner_h:
            self._card(
                cv, (58, banner_top, 1022, banner_top + banner_h), pal, radius=20
            )
            cv.rrect(
                (58, banner_top, 1022, banner_top + banner_h), 20, fill=c(gold, 36)
            )
            by = banner_top + 14
            for line in probe.wrap(source_note, font(18, 500), 850):
                cv.text(92, by, line, font(18, 500), gold)
                by += 28

        # outfit card (height adapts to the item count)
        self._card(cv, (58, outfit_top, 1022, outfit_top + oh), pal)
        cv.text(92, outfit_top + 32, "今日穿搭", font(28, 800), ink)
        cv.spaced(
            988, outfit_top + 40, "TODAY'S LOOK", font(16, 500), accent, 4, anchor="ra"
        )
        kf, vf = font(21, 400), font(22, 500)
        dot_colors = [accent, coral, gold]
        col_offsets = [0, 0]
        for i, (key, value) in enumerate(outfit):
            col_index = i // outfit_rows
            x = 92 + col_index * 456
            ry = outfit_top + 100 + col_offsets[col_index]
            col_offsets[col_index] += outfit_row_hs[i]
            cv.dot(x + 6, ry + 16, 4.5, c(dot_colors[i % 3], 220))
            if key:
                cv.text(x + 24, ry + 2, key, kf, sub)
            lines = outfit_lines[i]
            ly = ry + 1
            for line in lines[:2]:
                tx = x + 24 + (cv.tlen(key, kf) + 12 if key else 0)
                cv.text(tx, ly, line, vf, ink)
                ly += 28

        # plan card: rows adapt to wrapped text and center on their own axis
        self._card(cv, (58, p_top, 1022, p_top + p_h), pal)
        cv.text(92, p_top + 32, "今天要做的事", font(28, 800), ink)
        cv.spaced(988, p_top + 40, "DAY PLAN", font(16, 500), coral, 4, anchor="ra")
        tf, te = font(23, 700, "num"), font(16, 450, "num")
        tagf = font(17, 600)
        pill_cx = 988 - (cv.tlen("可回复", tagf) + 26) / 2
        yy = p_top + 108
        centers = []
        row_y = yy
        for rh in row_hs:
            centers.append(row_y + rh // 2)
            row_y += rh
        current_index = next((i for i, e in enumerate(entries) if e["current"]), None)
        if entries:
            cv.vline(140, centers[0], centers[-1], c(ink, 55), 2.5)
            if current_index is not None:
                cv.vline(140, centers[0], centers[current_index], c(accent, 220), 2.5)
        row_y = yy
        for i, e in enumerate(entries):
            rh = row_hs[i]
            cy = centers[i]
            current = bool(e["current"])
            if current:
                cv.rrect(
                    (76, row_y - 2, 1004, row_y + rh + 2),
                    16,
                    fill=c(accent, 40),
                    outline=c(accent, 140),
                    width=1.5,
                )
            if e["busy"]:
                cv.dot(140, cy, 8, c(coral))
                cv.ring(140, cy, 11, pal["tint"], 2.5)
            else:
                cv.ring(140, cy, 7, c(accent), 3)
            cv.text(176, cy - 26, e["start"], tf, accent if current else ink)
            if e["end"] and e["end"] != "—":
                cv.text(176, cy + 7, e["end"], te, sub)
            ly = cy - (len(plan_lines[i]) * 36) // 2 + 2
            for line in plan_lines[i]:
                cv.text(300, ly, line, af, ink)
                ly += 36
            if current:
                cv.pill(
                    pill_cx,
                    cy,
                    "当前",
                    tagf,
                    "#FFFFFF",
                    c(accent),
                    padx=14,
                    pady=7,
                    anchor="ma",
                )
            else:
                label = "忙碌" if e["busy"] else "可回复"
                cv.pill(
                    pill_cx,
                    cy,
                    label,
                    tagf,
                    c(coral if e["busy"] else accent),
                    c(coral if e["busy"] else accent, 30),
                    padx=13,
                    pady=6,
                    anchor="ma",
                )
            row_y += rh

        cv.spaced(84, yf, "LINGXI · BUSY SCHEDULE", font(16, 500), sub, 5)
        cv.text(996, yf - 2, f"{now:%H:%M} 更新", font(19, 450), sub, anchor="ra")
        return cv.finish(int(yf + 46))

    # -- status image -------------------------------------------------------
    def _render_status(
        self, status: BusyStatusImageData, now: datetime, theme: str
    ) -> bytes:
        pal = self._palette(theme)
        ink, sub = pal["ink"], pal["sub"]
        accent, coral, gold = pal["accent"], pal["coral"], pal["gold"]

        probe = Canvas(height=8)
        activity = status.activity or (
            "暂时无法回复" if status.is_busy else "现在可以正常回复消息"
        )
        act_lines = probe.wrap(activity, font(24, 450), 860)[:4]
        meta = []
        if status.remaining_minutes is not None:
            meta.append(f"预计还需 {status.remaining_minutes} 分钟")
        if status.current_start and status.current_end:
            meta.append(f"{status.current_start} - {status.current_end}")
        details = [
            ("下一忙碌时段", status.next_busy or "本周期内暂无后续忙碌时段", gold),
            (
                "聊天保护",
                status.chat_protection or "当前未启用保护倒计时",
                pal["accent"],
            ),
            (
                "消息队列",
                (
                    f"{status.queued_messages} 条消息 · {status.queued_users} 个会话"
                    if status.queued_messages
                    else "当前没有待处理消息"
                ),
                coral,
            ),
        ]
        value_f = font(23, 600)
        detail_rows = []
        for label, value, col in details:
            lines = probe.wrap(value, value_f, 620)
            detail_rows.append((label, lines, col, len(lines) * 35 + 56))

        state_h = 132 + len(act_lines) * 36 + (64 if meta else 24)
        s_top = 264
        d_top = s_top + state_h + 24
        total_h = sum(r[3] for r in detail_rows) + 24 * (len(detail_rows) - 1)
        yf = d_top + total_h + 40

        cv = Canvas(
            height=int(yf + 46) + 40, bg="#F0E6F8" if theme == "day" else "#171331"
        )
        cv.bg_gradient(pal["stops"])
        for col, gx, gy, gr, ga in pal["glows"]:
            cv.glow(gx, gy, gr, c(col), ga)

        self._header(
            cv,
            pal,
            "忙碌状态",
            f"{now:%Y.%m.%d · %H:%M}",
            "VIOLET · BUSY STATUS",
        )

        # current state card
        state_col = coral if status.is_busy else accent
        self._card(cv, (58, s_top, 1022, s_top + state_h), pal)
        cv.dot(104, s_top + 56, 8, c(state_col))
        cv.text(
            132,
            s_top + 34,
            "当前忙碌" if status.is_busy else "当前在线",
            font(30, 700),
            ink,
        )
        chip_text = "BUSY" if status.is_busy else "ONLINE"
        cv.pill(
            988,
            s_top + 30,
            chip_text,
            font(17, 700, "num"),
            c(state_col),
            c(state_col, 30),
            padx=16,
            pady=8,
            anchor="ra",
        )
        ay = s_top + 98
        for line in act_lines:
            cv.text(100, ay, line, font(24, 450), ink)
            ay += 36
        if meta:
            my = ay + 10
            mx = 100
            for text in meta:
                f = font(18, 600)
                w = cv.tlen(text, f) + 32
                cv.rrect((mx, my, mx + w, my + 40), 20, fill=c(gold, 34))
                cv.text(mx + 16, my + 9, text, f, c(gold))
                mx += w + 14

        # detail rows (content vertically centered in each card)
        y = d_top
        label_f = font(20, 450)
        for label, lines, col, rh in detail_rows:
            self._card(cv, (58, y, 1022, y + rh), pal, radius=24)
            cy = y + rh / 2
            cv.rrect((92, cy - 10, 102, cy + 10), 3, fill=c(col))
            cv.text(116, cy, label, label_f, sub, anchor="lm")
            ly = cy - (len(lines) * 35) / 2 + 4
            for line in lines:
                cv.text(320, ly, line, value_f, ink)
                ly += 35
            y += rh + 24

        cv.spaced(84, yf, "LINGXI · BUSY STATUS", font(16, 500), sub, 5)
        cv.text(996, yf - 2, f"{now:%H:%M} 更新", font(19, 450), sub, anchor="ra")
        return cv.finish(int(yf + 46))
