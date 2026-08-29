from datetime import date

from astrbot_plugin_busy_schedule.core.weather import (
    WeatherHour,
    _precipitation_summary,
)


def wet_hour(time_text: str) -> WeatherHour:
    return WeatherHour(
        time=time_text, temperature_c=25.0, condition="小雨", precipitation_mm=1.2
    )


def test_precipitation_summary_omits_date_for_base_day():
    hours = [wet_hour(f"2026-08-29T{hour:02d}:00") for hour in (7, 8, 15)]

    summary = _precipitation_summary(hours, date(2026, 8, 29))

    assert summary == "主要降水时段 07:00~09:00、15:00~16:00"


def test_precipitation_summary_keeps_date_for_other_days():
    hours = [wet_hour("2026-08-30T01:00"), wet_hour("2026-08-30T02:00")]

    summary = _precipitation_summary(hours, date(2026, 8, 29))

    assert summary == "主要降水时段 08-30 01:00~03:00"


def test_precipitation_summary_without_wet_hours():
    hours = [WeatherHour(time="2026-08-29T10:00", temperature_c=30.0, condition="晴")]

    assert _precipitation_summary(hours, date(2026, 8, 29)) == "无明显降水时段"
