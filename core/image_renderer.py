"""Local Pillow renderer for busy schedule command images."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


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
        if theme == "night":
            image = self._render_night_schedule(
                data, local_now, is_busy, entries, source_note, calendar_context
            )
        else:
            image = self._render_day_schedule(
                data, local_now, is_busy, entries, source_note, calendar_context
            )
        return self._encode_png(image)

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
        if self.resolve_theme(mode, now) == "night":
            image = self._render_night_status(status, now)
        else:
            image = self._render_day_status(status, now)
        return self._encode_png(image)

    def _font(self, size: int, bold: bool = False):
        candidates = []
        if bold:
            candidates.extend(
                [
                    Path(r"C:\Windows\Fonts\Dengb.ttf"),
                    Path(r"C:\Windows\Fonts\msyhbd.ttc"),
                    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
                    Path("/System/Library/Fonts/PingFang.ttc"),
                ]
            )
        candidates.extend(
            [
                Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf"),
                Path(r"C:\Windows\Fonts\msyh.ttc"),
                Path(r"C:\Windows\Fonts\simhei.ttf"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
                Path("/System/Library/Fonts/PingFang.ttc"),
            ]
        )
        for path in candidates:
            if path.is_file():
                return ImageFont.truetype(str(path), size)
        return ImageFont.load_default(size=size)

    def _fonts(self) -> dict[str, Any]:
        return {
            "hero": self._font(56, True),
            "title": self._font(42, True),
            "section": self._font(32, True),
            "body": self._font(28),
            "body_bold": self._font(28, True),
            "small": self._font(23),
            "small_bold": self._font(23, True),
            "tiny": self._font(19),
            "time": self._font(24, True),
        }

    def _load_avatar(self, size: int, border: tuple[int, int, int, int]):
        # Read on every render so replacing logo.png takes effect immediately.
        try:
            source = Image.open(self.plugin_dir / "logo.png").convert("RGB")
        except Exception:
            return None
        source = ImageOps.fit(
            source,
            (size, size),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.42),
        )
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        result = Image.new("RGBA", (size + 12, size + 12), (0, 0, 0, 0))
        ImageDraw.Draw(result).ellipse((0, 0, size + 11, size + 11), fill=border)
        result.paste(source, (6, 6), mask)
        return result

    @staticmethod
    def _rounded(draw, box, radius, fill, outline=None, width=1):
        draw.rounded_rectangle(
            box, radius=radius, fill=fill, outline=outline, width=width
        )

    @staticmethod
    def _shadow(image, box, radius=28, blur=18, offset=(0, 8)):
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        x1, y1, x2, y2 = box
        ox, oy = offset
        draw.rounded_rectangle(
            (x1 + ox, y1 + oy, x2 + ox, y2 + oy),
            radius=radius,
            fill=(30, 40, 60, 28),
        )
        image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur)))

    @staticmethod
    def _text_width(draw, text, font):
        return draw.textbbox((0, 0), text, font=font)[2]

    def _ellipsize(self, draw, text: str, font, max_width: int) -> str:
        """Keep user-defined observance names inside the image header."""
        if self._text_width(draw, text, font) <= max_width:
            return text
        suffix = "…"
        shortened = text
        while (
            shortened and self._text_width(draw, shortened + suffix, font) > max_width
        ):
            shortened = shortened[:-1]
        return shortened + suffix

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

    @staticmethod
    def _centered_text_xy(draw, box, text, font):
        left, top, right, bottom = box
        bounds = draw.textbbox((0, 0), text, font=font)
        width = bounds[2] - bounds[0]
        height = bounds[3] - bounds[1]
        return (
            left + (right - left - width) / 2 - bounds[0],
            top + (bottom - top - height) / 2 - bounds[1],
        )

    def _wrap(self, draw, text: str, font, max_width: int) -> list[str]:
        lines = []
        for paragraph in str(text).splitlines() or [""]:
            if not paragraph:
                lines.append("")
                continue
            line = ""
            for char in paragraph:
                candidate = line + char
                if line and self._text_width(draw, candidate, font) > max_width:
                    lines.append(line)
                    line = char
                else:
                    line = candidate
            if line:
                lines.append(line)
        return lines or [""]

    @staticmethod
    def _multiline(draw, xy, lines, font, fill, spacing=10):
        x, y = xy
        for line in lines:
            draw.text((x, y), line, font=font, fill=fill)
            y += font.size + spacing
        return y

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

    def _measure_rows(
        self,
        draw,
        entries,
        width,
        font,
        base,
        line_step,
        text_top,
        bottom_padding,
    ):
        measured = []
        for entry in entries:
            lines = self._wrap(draw, entry["activity"], font, width)
            text_height = (len(lines) - 1) * line_step + font.size
            height = max(base, text_top + text_height + bottom_padding)
            measured.append((entry, lines, height))
        return measured

    @staticmethod
    def _outfit_items(data: Any) -> list[str]:
        items = [
            line.strip()
            for line in str(getattr(data, "outfit", "")).splitlines()
            if line.strip()
        ]
        hairstyle = str(getattr(data, "hairstyle", "") or "").strip()
        if hairstyle:
            items.append(f"发型：{hairstyle}")
        return items or ["今日穿搭暂未设置"]

    @staticmethod
    def _weather(data: Any) -> tuple[str, str, str]:
        weather = getattr(data, "weather", None)
        if weather is None:
            return "天气暂不可用", "--℃", "暂无天气数据"
        location = str(getattr(weather, "display_location", "天气")).split("，", 1)[0]
        minimum = getattr(weather, "temperature_min_c", None)
        maximum = getattr(weather, "temperature_max_c", None)
        temperature = (
            f"{minimum:g}~{maximum:g}℃"
            if minimum is not None and maximum is not None
            else "--℃"
        )
        note = str(getattr(weather, "summary", "") or "无明显降水时段")
        return location, temperature, note

    @staticmethod
    def _weekday(value: date) -> str:
        return ("一", "二", "三", "四", "五", "六", "日")[value.weekday()]

    def _render_day_schedule(
        self, data, now, is_busy, entries, source_note, calendar_context=None
    ):
        fonts = self._fonts()
        probe = ImageDraw.Draw(Image.new("RGB", (self.width, 100)))
        outfit = self._outfit_items(data)
        midpoint = (len(outfit) + 1) // 2
        columns = (outfit[:midpoint], outfit[midpoint:])
        outfit_columns = []
        for column in columns:
            measured_column = []
            for value in column:
                lines = self._wrap(probe, value, fonts["small"], 380)
                text_height = (len(lines) - 1) * 34 + fonts["small"].size
                measured_column.append((lines, max(46, text_height + 10)))
            outfit_columns.append(measured_column)
        rows = self._measure_rows(
            probe,
            entries,
            700,
            fonts["body"],
            base=103,
            line_step=40,
            text_top=51,
            bottom_padding=24,
        )
        source_height = 48 if source_note else 0
        outfit_content_height = max(
            (sum(item[1] for item in column) for column in outfit_columns),
            default=0,
        )
        outfit_height = 112 + outfit_content_height + 44
        timeline_height = 126 + sum(row[2] for row in rows) + 30
        card_gap = 34
        height = 300 + source_height + outfit_height + card_gap + timeline_height + 100

        image = Image.new("RGBA", (self.width, height), "#F5F7FB")
        draw = ImageDraw.Draw(image)
        for y in range(0, height, 32):
            draw.line((0, y, self.width, y), fill="#E9EDF5", width=1)
        draw.rectangle((0, 0, 18, height), fill="#FF8E88")
        draw.rectangle((18, 0, 28, height), fill="#87BDE8")
        self._rounded(draw, (58, 46, 1022, 265), 34, "#FFFFFF")
        avatar = self._load_avatar(140, (135, 189, 232, 255))
        if avatar:
            image.alpha_composite(avatar, (82, 78))
        display_date = date.fromisoformat(str(data.date))
        draw.text((264, 67), "小怡的今日行程", font=fonts["hero"], fill="#27324A")
        date_line = (
            f"{display_date:%Y.%m.%d}  ·  星期{self._weekday(display_date)}"
            f"  ·  {self._calendar_observance(calendar_context)}"
        )
        draw.text(
            (267, 137),
            self._ellipsize(draw, date_line, fonts["small"], 455),
            font=fonts["small"],
            fill="#69748B",
        )
        status_fill = "#FFEAE8" if is_busy else "#E7F4FF"
        status_color = "#D85B52" if is_busy else "#236FA8"
        status_text = "忙碌中 · 暂缓回复" if is_busy else "在线 · 可回消息"
        status_box = (267, 184, 485, 222)
        self._rounded(draw, status_box, 19, status_fill)
        text_bounds = draw.textbbox((0, 0), status_text, font=fonts["tiny"])
        text_width = text_bounds[2] - text_bounds[0]
        dot_size = 10
        dot_gap = 12
        group_width = dot_size + dot_gap + text_width
        group_left = status_box[0] + (status_box[2] - status_box[0] - group_width) / 2
        group_center_y = (status_box[1] + status_box[3]) / 2
        draw.ellipse(
            (
                group_left,
                group_center_y - dot_size / 2,
                group_left + dot_size,
                group_center_y + dot_size / 2,
            ),
            fill=status_color,
        )
        text_x = group_left + dot_size + dot_gap
        text_y = group_center_y - (text_bounds[3] - text_bounds[1]) / 2 - text_bounds[1]
        draw.text(
            (text_x, text_y),
            status_text,
            font=fonts["tiny"],
            fill=status_color,
        )
        location, temperature, weather_note = self._weather(data)
        self._rounded(draw, (760, 79, 978, 220), 28, "#FFF4C9")
        draw.text(
            (788, 96), f"{location} · 今日天气", font=fonts["tiny"], fill="#806A27"
        )
        draw.text((788, 134), temperature, font=fonts["section"], fill="#4E4324")
        draw.text((788, 180), weather_note, font=fonts["tiny"], fill="#806A27")

        top = 300
        if source_note:
            self._rounded(draw, (58, top, 1022, top + 38), 12, "#FFF4C9")
            draw.text((82, top + 6), source_note, font=fonts["tiny"], fill="#806A27")
            top += source_height
        outfit_bottom = top + outfit_height
        self._shadow(image, (58, top, 1022, outfit_bottom))
        draw = ImageDraw.Draw(image)
        self._rounded(draw, (58, top, 1022, outfit_bottom), 28, "#FFFFFF")
        draw.text((92, top + 28), "TODAY'S LOOK", font=fonts["tiny"], fill="#E06A65")
        draw.text((92, top + 62), "今日穿搭", font=fonts["section"], fill="#27324A")
        for column_index, column in enumerate(outfit_columns):
            y = top + 112
            x = 92 + column_index * 458
            for lines, row_height in column:
                draw.ellipse((x + 6, y + 9, x + 16, y + 19), fill="#87BDE8")
                self._multiline(
                    draw, (x + 34, y), lines, fonts["small"], "#4D576D", spacing=11
                )
                y += row_height

        timeline_top = outfit_bottom + card_gap
        self._rounded(
            draw,
            (58, timeline_top, 1022, timeline_top + timeline_height),
            30,
            "#FFFFFF",
        )
        draw.text(
            (92, timeline_top + 30), "DAY PLAN", font=fonts["tiny"], fill="#2D8CD5"
        )
        draw.text(
            (92, timeline_top + 66),
            "今天要做的事",
            font=fonts["section"],
            fill="#27324A",
        )
        y = timeline_top + 126
        rail_x = 256
        draw.line(
            (rail_x, y, rail_x, timeline_top + timeline_height - 38),
            fill="#DDE4F0",
            width=5,
        )
        for entry, lines, row_height in rows:
            color = "#E76E68" if entry["busy"] else "#3C98D4"
            if entry["current"]:
                self._rounded(
                    draw,
                    (80, y - 8, 994, y + row_height - 8),
                    22,
                    "#EEF7FF",
                    outline="#87BDE8",
                    width=3,
                )
                draw.text((844, y + 14), "当前", font=fonts["tiny"], fill="#2878B5")
            draw.text((98, y + 10), entry["start"], font=fonts["time"], fill="#27324A")
            draw.text((98, y + 43), entry["end"], font=fonts["tiny"], fill="#8A93A5")
            draw.ellipse((rail_x - 11, y + 24, rail_x + 11, y + 46), fill=color)
            tag_box = (294, y + 8, 400, y + 42)
            self._rounded(
                draw,
                tag_box,
                17,
                "#FFEAE8" if entry["busy"] else "#E7F4FF",
            )
            tag_text = "忙碌" if entry["busy"] else "可回复"
            draw.text(
                self._centered_text_xy(draw, tag_box, tag_text, fonts["tiny"]),
                tag_text,
                font=fonts["tiny"],
                fill=color,
            )
            self._multiline(
                draw, (294, y + 51), lines, fonts["body"], "#333C50", spacing=12
            )
            y += row_height
        footer_y = timeline_top + timeline_height + 30
        draw.text(
            (58, footer_y),
            "LINGXI  ·  BUSY SCHEDULE",
            font=fonts["tiny"],
            fill="#8A93A5",
        )
        draw.text(
            (856, footer_y), f"{now:%H:%M} 更新", font=fonts["tiny"], fill="#8A93A5"
        )
        return image.convert("RGB")

    def _render_night_schedule(
        self, data, now, is_busy, entries, source_note, calendar_context=None
    ):
        fonts = self._fonts()
        probe = ImageDraw.Draw(Image.new("RGB", (self.width, 100)))
        outfit_text = " / ".join(
            line.split("：", 1)[-1] for line in self._outfit_items(data)
        )
        outfit_lines = self._wrap(probe, outfit_text, fonts["small"], 760)
        rows = self._measure_rows(
            probe,
            entries,
            650,
            fonts["body"],
            base=98,
            line_step=40,
            text_top=13,
            bottom_padding=44,
        )
        source_height = 48 if source_note else 0
        header_height = 390 + source_height
        outfit_text_height = (len(outfit_lines) - 1) * 34 + fonts["small"].size
        outfit_card_height = max(72, 20 + outfit_text_height + 26)
        outfit_gap = 28
        outfit_height = outfit_card_height + outfit_gap
        timeline_height = 135 + sum(row[2] for row in rows)
        height = header_height + outfit_height + timeline_height + 100

        image = Image.new("RGBA", (self.width, height), "#17191D")
        draw = ImageDraw.Draw(image)
        for y in range(0, height, 64):
            draw.line((0, y, self.width, y), fill="#202329", width=1)
        draw.rectangle((0, 0, self.width, 18), fill="#F06F61")
        draw.text((58, 61), "DAILY", font=fonts["tiny"], fill="#80D7C5")
        draw.text((58, 92), "小怡的今日片单", font=fonts["hero"], fill="#F4F1EA")
        display_date = date.fromisoformat(str(data.date))
        date_line = (
            f"{display_date:%Y / %m / %d}  ·  星期{self._weekday(display_date)}"
            f"  ·  {self._calendar_observance(calendar_context)}"
        )
        draw.text(
            (61, 164),
            self._ellipsize(draw, date_line, fonts["small"], 720),
            font=fonts["small"],
            fill="#A7ABB3",
        )
        status_color = "#F06F61" if is_busy else "#80D7C5"
        status_fill = "#442B2B" if is_busy else "#263F3A"
        self._rounded(
            draw, (58, 214, 330, 260), 8, status_fill, outline=status_color, width=2
        )
        draw.ellipse((80, 230, 92, 242), fill=status_color)
        draw.text(
            (107, 223),
            "BUSY · 暂缓回复" if is_busy else "ONLINE · 可回消息",
            font=fonts["tiny"],
            fill=status_color,
        )
        avatar = self._load_avatar(150, (240, 111, 97, 255))
        if avatar:
            image.alpha_composite(avatar, (844, 56))
        location, temperature, weather_note = self._weather(data)
        self._rounded(draw, (58, 292, 1022, 364), 14, "#22252B")
        draw.text(
            (86, 309),
            f"{location}  {temperature}",
            font=fonts["body_bold"],
            fill="#F4C866",
        )
        draw.text((558, 313), weather_note, font=fonts["small"], fill="#A7ABB3")
        top = 390
        if source_note:
            self._rounded(draw, (58, top, 1022, top + 38), 10, "#453D27")
            draw.text((82, top + 6), source_note, font=fonts["tiny"], fill="#F4C866")
            top += source_height
        outfit_bottom = top + outfit_card_height
        self._rounded(
            draw,
            (58, top, 1022, outfit_bottom),
            18,
            "#22252B",
            outline="#343840",
            width=2,
        )
        draw.text((86, top + 24), "COSTUME", font=fonts["tiny"], fill="#F06F61")
        self._multiline(
            draw, (218, top + 20), outfit_lines, fonts["small"], "#D8D5CE", spacing=11
        )
        timeline_top = outfit_bottom + outfit_gap
        draw.text(
            (58, timeline_top + 8),
            "TODAY'S SCENES",
            font=fonts["title"],
            fill="#F4F1EA",
        )
        draw.text((786, timeline_top + 26), "BUSY", font=fonts["tiny"], fill="#F06F61")
        draw.text((903, timeline_top + 26), "OPEN", font=fonts["tiny"], fill="#80D7C5")
        y = timeline_top + 92
        for entry, lines, row_height in rows:
            color = "#F06F61" if entry["busy"] else "#80D7C5"
            fill = "#25292F" if entry["current"] else "#1D2025"
            self._rounded(
                draw,
                (58, y, 1022, y + row_height - 10),
                14,
                fill,
                outline=color if entry["current"] else "#30343B",
                width=3 if entry["current"] else 1,
            )
            draw.text(
                (80, y + 17),
                f"{entry['index']:02d}",
                font=fonts["tiny"],
                fill="#777D87",
            )
            draw.text((130, y + 14), entry["start"], font=fonts["time"], fill="#F4F1EA")
            draw.text((222, y + 17), entry["end"], font=fonts["tiny"], fill="#777D87")
            draw.rectangle((304, y + 18, 312, y + row_height - 28), fill=color)
            self._multiline(
                draw, (344, y + 13), lines, fonts["body"], "#E4E1DA", spacing=12
            )
            state = "BUSY" if entry["busy"] else "OPEN"
            draw.text((936, y + row_height - 43), state, font=fonts["tiny"], fill=color)
            if entry["current"]:
                draw.text(
                    (768, y + row_height - 43),
                    "NOW PLAYING",
                    font=fonts["tiny"],
                    fill="#F4C866",
                )
            y += row_height
        footer_y = timeline_top + timeline_height + 20
        draw.text(
            (58, footer_y), "LINGXI BUSY SCHEDULE", font=fonts["tiny"], fill="#777D87"
        )
        draw.text(
            (850, footer_y), f"UPDATED {now:%H:%M}", font=fonts["tiny"], fill="#777D87"
        )
        return image.convert("RGB")

    def _render_day_status(self, status: BusyStatusImageData, now: datetime):
        fonts = self._fonts()
        image = Image.new("RGBA", (self.width, 1050), "#F5F7FB")
        draw = ImageDraw.Draw(image)
        for y in range(0, 1050, 32):
            draw.line((0, y, self.width, y), fill="#E9EDF5", width=1)
        draw.rectangle((0, 0, 18, 1050), fill="#FF8E88")
        draw.rectangle((18, 0, 28, 1050), fill="#87BDE8")
        avatar = self._load_avatar(132, (135, 189, 232, 255))
        if avatar:
            image.alpha_composite(avatar, (78, 58))
        draw.text((250, 66), "忙碌状态", font=fonts["hero"], fill="#27324A")
        draw.text(
            (253, 139),
            f"{now:%Y.%m.%d  %H:%M}",
            font=fonts["small"],
            fill="#69748B",
        )
        color = "#E76E68" if status.is_busy else "#3C98D4"
        pale = "#FFF0EE" if status.is_busy else "#EAF5FC"
        self._shadow(image, (58, 238, 1022, 526), radius=30)
        draw = ImageDraw.Draw(image)
        self._rounded(draw, (58, 238, 1022, 526), 30, "#FFFFFF")
        draw.ellipse((92, 278, 122, 308), fill=color)
        draw.text(
            (146, 258),
            "当前忙碌" if status.is_busy else "当前在线",
            font=fonts["title"],
            fill="#27324A",
        )
        self._rounded(draw, (790, 265, 970, 313), 24, pale)
        draw.text(
            (832, 274),
            "BUSY" if status.is_busy else "ONLINE",
            font=fonts["small_bold"],
            fill=color,
        )
        activity = status.activity or (
            "暂时无法回复" if status.is_busy else "现在可以正常回复消息"
        )
        activity_lines = self._wrap(draw, activity, fonts["body"], 820)
        self._multiline(
            draw, (94, 337), activity_lines[:3], fonts["body"], "#4D576D", spacing=12
        )
        if status.remaining_minutes is not None:
            draw.text(
                (94, 454),
                f"预计还需 {status.remaining_minutes} 分钟",
                font=fonts["small_bold"],
                fill=color,
            )
            if status.current_start and status.current_end:
                draw.text(
                    (742, 454),
                    f"{status.current_start} - {status.current_end}",
                    font=fonts["small"],
                    fill="#69748B",
                )
        self._draw_day_status_detail(
            image,
            draw,
            58,
            566,
            "下一忙碌时段",
            status.next_busy or "本周期内暂无后续忙碌时段",
            "#F4C866",
            fonts,
        )
        self._draw_day_status_detail(
            image,
            draw,
            58,
            708,
            "聊天保护",
            status.chat_protection or "当前未启用保护倒计时",
            "#87BDE8",
            fonts,
        )
        queue_text = (
            f"{status.queued_messages} 条消息 · {status.queued_users} 个会话"
            if status.queued_messages
            else "当前没有待处理消息"
        )
        self._draw_day_status_detail(
            image, draw, 58, 850, "消息队列", queue_text, "#FF8E88", fonts
        )
        draw.text(
            (58, 1012), "LINGXI  ·  BUSY STATUS", font=fonts["tiny"], fill="#8A93A5"
        )
        return image.convert("RGB")

    def _draw_day_status_detail(self, image, draw, x, y, label, value, color, fonts):
        self._shadow(image, (x, y, 1022, y + 118), radius=22, blur=12, offset=(0, 5))
        draw = ImageDraw.Draw(image)
        self._rounded(draw, (x, y, 1022, y + 118), 22, "#FFFFFF")
        draw.rectangle((x, y, x + 10, y + 118), fill=color)
        draw.text((x + 34, y + 20), label, font=fonts["small"], fill="#69748B")
        lines = self._wrap(draw, value, fonts["body_bold"], 680)
        self._multiline(
            draw, (x + 245, y + 18), lines[:2], fonts["body_bold"], "#27324A", spacing=8
        )

    def _render_night_status(self, status: BusyStatusImageData, now: datetime):
        fonts = self._fonts()
        image = Image.new("RGBA", (self.width, 1050), "#17191D")
        draw = ImageDraw.Draw(image)
        for y in range(0, 1050, 64):
            draw.line((0, y, self.width, y), fill="#202329", width=1)
        draw.rectangle((0, 0, self.width, 18), fill="#F06F61")
        draw.text((58, 60), "CURRENT STATUS", font=fonts["tiny"], fill="#80D7C5")
        draw.text((58, 96), "忙碌状态", font=fonts["hero"], fill="#F4F1EA")
        draw.text(
            (61, 169),
            f"{now:%Y / %m / %d  ·  %H:%M}",
            font=fonts["small"],
            fill="#A7ABB3",
        )
        avatar = self._load_avatar(146, (240, 111, 97, 255))
        if avatar:
            image.alpha_composite(avatar, (844, 54))
        color = "#F06F61" if status.is_busy else "#80D7C5"
        fill = "#442B2B" if status.is_busy else "#263F3A"
        self._rounded(draw, (58, 236, 1022, 508), 24, "#22252B", outline=color, width=3)
        self._rounded(draw, (86, 268, 306, 316), 8, fill)
        draw.text(
            (118, 277),
            "BUSY · 忙碌中" if status.is_busy else "ONLINE · 可回复",
            font=fonts["small_bold"],
            fill=color,
        )
        activity = status.activity or (
            "暂时无法回复" if status.is_busy else "现在可以正常回复消息"
        )
        lines = self._wrap(draw, activity, fonts["body"], 840)
        self._multiline(
            draw, (86, 355), lines[:3], fonts["body"], "#E4E1DA", spacing=12
        )
        if status.remaining_minutes is not None:
            draw.text(
                (86, 451),
                f"REMAINING  {status.remaining_minutes} MIN",
                font=fonts["small_bold"],
                fill="#F4C866",
            )
            draw.text(
                (760, 451),
                f"{status.current_start} - {status.current_end}",
                font=fonts["small"],
                fill="#777D87",
            )
        details = [
            ("NEXT BUSY", status.next_busy or "本周期内暂无后续忙碌时段", "#F4C866"),
            ("CHAT GUARD", status.chat_protection or "当前未启用保护倒计时", "#80D7C5"),
            (
                "MESSAGE QUEUE",
                f"{status.queued_messages} 条消息 · {status.queued_users} 个会话"
                if status.queued_messages
                else "当前没有待处理消息",
                "#F06F61",
            ),
        ]
        y = 548
        for label, value, accent in details:
            self._rounded(
                draw, (58, y, 1022, y + 118), 16, "#1D2025", outline="#30343B"
            )
            draw.rectangle((58, y + 18, 66, y + 100), fill=accent)
            draw.text((90, y + 20), label, font=fonts["tiny"], fill=accent)
            value_lines = self._wrap(draw, value, fonts["body_bold"], 680)
            self._multiline(
                draw,
                (302, y + 18),
                value_lines[:2],
                fonts["body_bold"],
                "#E4E1DA",
                spacing=8,
            )
            y += 142
        draw.text((58, 1008), "LINGXI BUSY STATUS", font=fonts["tiny"], fill="#777D87")
        return image.convert("RGB")

    @staticmethod
    def _encode_png(image: Image.Image) -> bytes:
        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
