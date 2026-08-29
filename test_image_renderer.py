from datetime import datetime
from io import BytesIO
from types import SimpleNamespace

from astrbot_plugin_busy_schedule.core.image_renderer import (
    BusyScheduleImageRenderer,
    BusyStatusImageData,
)
from astrbot_plugin_busy_schedule.core.style_kit import Canvas, font
from PIL import Image


def make_schedule():
    weather = SimpleNamespace(
        display_location="上海，上海市，中国",
        temperature_min_c=25.0,
        temperature_max_c=32.0,
        summary="无明显降水时段",
    )
    return SimpleNamespace(
        date="2026-08-17",
        outfit=(
            "风格：日系少女风\n"
            "内衣：米白色无钢圈轻薄文胸\n"
            "内裤：浅粉色蕾丝边棉质内裤\n"
            "上装：白色短袖衬衫\n"
            "下装：浅灰色百褶短裙\n"
            "袜子：白色中筒袜\n"
            "鞋子：黑色小皮鞋\n"
            "配饰：细款银色项链"
        ),
        hairstyle="双马尾",
        schedule=(
            "08:00-09:00 小怡醒来洗漱【可回消息】\n"
            "09:00-12:00 小怡专心处理一项很长很长的工作内容【忙碌】\n"
            "12:00-23:30 小怡休息并随时查看消息【可回消息】\n"
            "23:30-00:30 小怡洗澡【忙碌】\n"
            "00:30 小怡睡觉【忙碌】"
        ),
        weather=weather,
    )


def open_png(payload: bytes) -> Image.Image:
    image = Image.open(BytesIO(payload))
    image.load()
    return image


def test_theme_mode_resolves_explicit_and_automatic_hours(tmp_path):
    renderer = BusyScheduleImageRenderer(tmp_path)

    assert renderer.resolve_theme("白天模式", datetime(2026, 8, 17, 23)) == "day"
    assert renderer.resolve_theme("夜间模式", datetime(2026, 8, 17, 12)) == "night"
    assert renderer.resolve_theme("自动切换", datetime(2026, 8, 17, 7)) == "day"
    assert renderer.resolve_theme("自动切换", datetime(2026, 8, 17, 18, 59)) == "day"
    assert renderer.resolve_theme("自动切换", datetime(2026, 8, 17, 19)) == "night"
    assert renderer.resolve_theme("invalid", datetime(2026, 8, 17, 6, 59)) == "night"


def test_schedule_renders_day_and_night_png_without_logo(tmp_path):
    renderer = BusyScheduleImageRenderer(tmp_path)
    schedule = make_schedule()

    day = open_png(
        renderer.render_schedule(schedule, datetime(2026, 8, 17, 10), False, "白天模式")
    )
    night = open_png(
        renderer.render_schedule(
            schedule, datetime(2026, 8, 17, 23, 45), True, "夜间模式"
        )
    )

    assert day.mode == night.mode == "RGB"
    assert day.width == night.width == 1080
    assert day.height > 1200
    assert night.height > 1200
    assert day.getpixel((500, 20)) != night.getpixel((500, 20))


def test_replacing_logo_changes_next_render(tmp_path):
    renderer = BusyScheduleImageRenderer(tmp_path)
    schedule = make_schedule()
    logo_path = tmp_path / "logo.png"
    Image.new("RGB", (300, 300), "red").save(logo_path)
    first = renderer.render_schedule(
        schedule, datetime(2026, 8, 17, 10), False, "白天模式"
    )

    Image.new("RGB", (300, 300), "blue").save(logo_path)
    second = renderer.render_schedule(
        schedule, datetime(2026, 8, 17, 10), False, "白天模式"
    )

    assert first != second


def test_day_outfit_card_grows_for_future_hairstyle_content(tmp_path):
    renderer = BusyScheduleImageRenderer(tmp_path)
    schedule = make_schedule()
    schedule.hairstyle = ""
    without_hairstyle = open_png(
        renderer.render_schedule(schedule, datetime(2026, 8, 17, 10), False, "白天模式")
    )

    schedule.hairstyle = "蓬松双马尾，搭配浅蓝色丝带和珍珠发夹"
    with_hairstyle = open_png(
        renderer.render_schedule(schedule, datetime(2026, 8, 17, 10), False, "白天模式")
    )

    assert 40 <= with_hairstyle.height - without_hairstyle.height <= 60


def test_multiline_activity_row_reserves_additional_bottom_spacing(tmp_path):
    renderer = BusyScheduleImageRenderer(tmp_path)
    short = make_schedule()
    short.schedule = "10:00-11:00 短活动 【可回复】"
    long_schedule = make_schedule()
    long_schedule.schedule = (
        "10:00-11:00 这是一段会稳定换成多行的很长活动描述" * 4 + " 【可回复】"
    )

    short_png = open_png(
        renderer.render_schedule(short, datetime(2026, 8, 17, 10), False, "白天模式")
    )
    long_png = open_png(
        renderer.render_schedule(
            long_schedule, datetime(2026, 8, 17, 10), False, "白天模式"
        )
    )

    assert long_png.height >= short_png.height + 36


def test_status_renders_both_themes(tmp_path):
    renderer = BusyScheduleImageRenderer(tmp_path)
    status = BusyStatusImageData(
        is_busy=True,
        activity="小怡正在专心看电影，暂时没有查看消息",
        remaining_minutes=37,
        current_start="21:10",
        current_end="22:47",
        next_busy="08-18 00:30-08-18 07:43 小怡睡觉",
        chat_protection="距保护结束约 8 分钟",
        queued_messages=4,
        queued_users=2,
    )

    day = open_png(
        renderer.render_status(status, datetime(2026, 8, 17, 12), "白天模式")
    )
    night = open_png(
        renderer.render_status(status, datetime(2026, 8, 17, 22), "夜间模式")
    )

    assert day.width == night.width == 1080
    assert day.height == night.height > 800
    assert day.mode == night.mode == "RGB"
    assert day.getpixel((500, 20)) != night.getpixel((500, 20))


def test_weather_note_lines_wrap_and_truncate(tmp_path):
    renderer = BusyScheduleImageRenderer(tmp_path)
    probe = Canvas(height=8)
    note_font = font(15, 450)

    single = renderer._note_lines(probe, "主要降水时段 07:00~09:00", note_font, 120)
    assert single == ["降水 07:00~09:00"]

    double = renderer._note_lines(
        probe, "主要降水时段 07:00~09:00、15:00~16:30", note_font, 120
    )
    assert len(double) == 2
    assert all(probe.tlen(line, note_font) <= 120 for line in double)

    triple = renderer._note_lines(
        probe, "主要降水时段 07:00~09:00、11:00~13:00、20:00~22:00", note_font, 120
    )
    assert len(triple) == 2
    assert triple[-1].endswith("…")
    assert all(probe.tlen(line, note_font) <= 120 for line in triple)


def test_schedule_renders_with_multiline_precipitation_note(tmp_path):
    renderer = BusyScheduleImageRenderer(tmp_path)
    schedule = make_schedule()
    schedule.weather.summary = "主要降水时段 07:00~09:00、11:00~13:00、20:00~22:00"

    day = open_png(
        renderer.render_schedule(schedule, datetime(2026, 8, 29, 10), False, "白天模式")
    )
    night = open_png(
        renderer.render_schedule(schedule, datetime(2026, 8, 29, 21), True, "夜间模式")
    )

    assert day.width == night.width == 1080
    assert day.height > 1200
    assert night.height > 1200
