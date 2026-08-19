"""Calendar context for schedule generation and chat prompt injection."""

import logging
from datetime import date

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - allows standalone calendar tests
    logger = logging.getLogger("busy_schedule.calendar")

_WEEKDAY_CN = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
_WEEKDAY_SHORT = ("一", "二", "三", "四", "五", "六", "日")
_WEEKDAY_LOOKUP = {
    "星期一": 0,
    "周一": 0,
    "礼拜一": 0,
    "星期二": 1,
    "周二": 1,
    "礼拜二": 1,
    "星期三": 2,
    "周三": 2,
    "礼拜三": 2,
    "星期四": 3,
    "周四": 3,
    "礼拜四": 3,
    "星期五": 4,
    "周五": 4,
    "礼拜五": 4,
    "星期六": 5,
    "周六": 5,
    "礼拜六": 5,
    "星期日": 6,
    "星期天": 6,
    "周日": 6,
    "周天": 6,
    "礼拜天": 6,
    "礼拜日": 6,
}
_SOLAR_HOLIDAYS = {
    (1, 1): "元旦",
    (2, 14): "情人节",
    (3, 8): "妇女节",
    (3, 14): "白色情人节",
    (4, 1): "愚人节",
    (5, 1): "劳动节",
    (5, 4): "青年节",
    (5, 20): "网络情人节",
    (6, 1): "儿童节",
    (8, 14): "绿色情人节",
    (9, 10): "教师节",
    (10, 1): "国庆节",
    (10, 31): "万圣节",
    (11, 11): "双十一",
    (12, 24): "平安夜",
    (12, 25): "圣诞节",
}
_LUNAR_HOLIDAYS = {
    (1, 1): "春节",
    (1, 15): "元宵节",
    (2, 2): "龙抬头",
    (5, 5): "端午节",
    (7, 7): "七夕节",
    (7, 15): "中元节",
    (8, 15): "中秋节",
    (9, 9): "重阳节",
    (12, 8): "腊八节",
    (12, 23): "小年",
    (12, 24): "小年",
}
_LUNAR_INFO = (
    0x04BD8,
    0x04AE0,
    0x0A570,
    0x054D5,
    0x0D260,
    0x0D950,
    0x16554,
    0x056A0,
    0x09AD0,
    0x055D2,
    0x04AE0,
    0x0A5B6,
    0x0A4D0,
    0x0D250,
    0x1D255,
    0x0B540,
    0x0D6A0,
    0x0ADA2,
    0x095B0,
    0x14977,
    0x04970,
    0x0A4B0,
    0x0B4B5,
    0x06A50,
    0x06D40,
    0x1AB54,
    0x02B60,
    0x09570,
    0x052F2,
    0x04970,
    0x06566,
    0x0D4A0,
    0x0EA50,
    0x06E95,
    0x05AD0,
    0x02B60,
    0x186E3,
    0x092E0,
    0x1C8D7,
    0x0C950,
    0x0D4A0,
    0x1D8A6,
    0x0B550,
    0x056A0,
    0x1A5B4,
    0x025D0,
    0x092D0,
    0x0D2B2,
    0x0A950,
    0x0B557,
    0x06CA0,
    0x0B550,
    0x15355,
    0x04DA0,
    0x0A5D0,
    0x14573,
    0x052D0,
    0x0A9A8,
    0x0E950,
    0x06AA0,
    0x0AEA6,
    0x0AB50,
    0x04B60,
    0x0AAE4,
    0x0A570,
    0x05260,
    0x0F263,
    0x0D950,
    0x05B57,
    0x056A0,
    0x096D0,
    0x04DD5,
    0x04AD0,
    0x0A4D0,
    0x0D4D4,
    0x0D250,
    0x0D558,
    0x0B540,
    0x0B5A0,
    0x195A6,
    0x095B0,
    0x049B0,
    0x0A974,
    0x0A4B0,
    0x0B27A,
    0x06A50,
    0x06D40,
    0x0AF46,
    0x0AB60,
    0x09570,
    0x04AF5,
    0x04970,
    0x064B0,
    0x074A3,
    0x0EA50,
    0x06B58,
    0x05AC0,
    0x0AB60,
    0x096D5,
    0x092E0,
    0x0C960,
    0x0D954,
    0x0D4A0,
    0x0DA50,
    0x07552,
    0x056A0,
    0x0ABB7,
    0x025D0,
    0x092D0,
    0x0CAB5,
    0x0A950,
    0x0B4A0,
    0x0BAA4,
    0x0AD50,
    0x055D9,
    0x04BA0,
    0x0A5B0,
    0x15176,
    0x052B0,
    0x0A930,
    0x07954,
    0x06AA0,
    0x0AD50,
    0x05B52,
    0x04B60,
    0x0A6E6,
    0x0A4E0,
    0x0D260,
    0x0EA65,
    0x0D530,
    0x05AA0,
    0x076A3,
    0x096D0,
    0x04AFB,
    0x04AD0,
    0x0A4D0,
    0x1D0B6,
    0x0D250,
    0x0D520,
    0x0DD45,
    0x0B5A0,
    0x056D0,
    0x055B2,
    0x049B0,
    0x0A577,
    0x0A4B0,
    0x0AA50,
    0x1B255,
    0x06D20,
    0x0ADA0,
    0x14B63,
    0x09370,
    0x049F8,
    0x04970,
    0x064B0,
    0x168A6,
    0x0EA50,
    0x06AA0,
    0x1A6C4,
    0x0AAE0,
    0x092E0,
    0x0D2E3,
    0x0C960,
    0x0D557,
    0x0D4A0,
    0x0DA50,
    0x05D55,
    0x056A0,
    0x0A6D0,
    0x055D4,
    0x052D0,
    0x0A9B8,
    0x0A950,
    0x0B4A0,
    0x0B6A6,
    0x0AD50,
    0x055A0,
    0x0ABA4,
    0x0A5B0,
    0x052B0,
    0x0B273,
    0x06930,
    0x07337,
    0x06AA0,
    0x0AD50,
    0x14B55,
    0x04B60,
    0x0A570,
    0x054E4,
    0x0D160,
    0x0E968,
    0x0D520,
    0x0DAA0,
    0x16AA6,
    0x056D0,
    0x04AE0,
    0x0A9D4,
    0x0A2D0,
    0x0D150,
    0x0F252,
)
_LUNAR_START_DATE = date(1900, 1, 31)
_LUNAR_YEAR_DAYS = tuple(
    348
    + sum(1 for month in range(1, 13) if year_info & (0x10000 >> month))
    + (30 if year_info & 0x10000 else 29 if year_info & 0xF else 0)
    for year_info in _LUNAR_INFO
)


def _cfg_lookup(config: object, key: str, default=None):
    if not isinstance(config, dict):
        return default
    for group_name in ("基础设置", "忙碌时段", "关键词设置", "消息合并", "日程生成"):
        group = config.get(group_name, {})
        if isinstance(group, dict):
            value = group.get(key)
            if value not in (None, "", {}, []):
                return value
    value = config.get(key)
    if value not in (None, "", {}, []):
        return value
    return default


def _safe_int(value: object, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _normalize_weekday(value: object) -> int | None:
    if isinstance(value, int):
        if 1 <= value <= 7:
            return value - 1
        if 0 <= value <= 6:
            return value
        return None
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return _normalize_weekday(int(text))
    return _WEEKDAY_LOOKUP.get(text)


def _lunar_month_days(year_index: int, month: int) -> int:
    return 30 if _LUNAR_INFO[year_index] & (0x10000 >> month) else 29


def _lunar_leap_month(year_index: int) -> int:
    return _LUNAR_INFO[year_index] & 0xF


def _lunar_leap_days(year_index: int) -> int:
    leap_month = _lunar_leap_month(year_index)
    if not leap_month:
        return 0
    return 30 if _LUNAR_INFO[year_index] & 0x10000 else 29


def _solar_to_lunar(date_obj: date) -> tuple[int, int, int, bool] | None:
    offset = (date_obj - _LUNAR_START_DATE).days
    if offset < 0:
        return None

    year_index = 0
    for year_index, year_days in enumerate(_LUNAR_YEAR_DAYS):
        if offset < year_days:
            break
        offset -= year_days
    else:
        return None

    lunar_year = 1900 + year_index
    leap_month = _lunar_leap_month(year_index)

    for month in range(1, 13):
        month_days = _lunar_month_days(year_index, month)
        if offset < month_days:
            return lunar_year, month, offset + 1, False
        offset -= month_days
        if leap_month == month:
            leap_days = _lunar_leap_days(year_index)
            if offset < leap_days:
                return lunar_year, month, offset + 1, True
            offset -= leap_days

    return None


def _lunar_month_cn(month: int, is_leap: bool = False) -> str:
    months = [
        "正月",
        "二月",
        "三月",
        "四月",
        "五月",
        "六月",
        "七月",
        "八月",
        "九月",
        "十月",
        "冬月",
        "腊月",
    ]
    if 1 <= month <= 12:
        return f"闰{months[month - 1]}" if is_leap else months[month - 1]
    return ""


def _lunar_day_cn(day: int) -> str:
    days = [
        "初一",
        "初二",
        "初三",
        "初四",
        "初五",
        "初六",
        "初七",
        "初八",
        "初九",
        "初十",
        "十一",
        "十二",
        "十三",
        "十四",
        "十五",
        "十六",
        "十七",
        "十八",
        "十九",
        "二十",
        "廿一",
        "廿二",
        "廿三",
        "廿四",
        "廿五",
        "廿六",
        "廿七",
        "廿八",
        "廿九",
        "三十",
    ]
    if 1 <= day <= 30:
        return days[day - 1]
    return ""


def get_lunar_date_cn(date_obj: date) -> str:
    """Return the Chinese lunar date string for a solar date."""
    lunar = _solar_to_lunar(date_obj)
    if not lunar:
        return ""
    _year, month, day, is_leap = lunar
    month_text = _lunar_month_cn(month, is_leap)
    day_text = _lunar_day_cn(day)
    if not month_text or not day_text:
        return ""
    return f"农历{month_text}{day_text}"


def get_holiday(date_obj: date) -> str:
    """Return known holiday names for a solar date.

    The optional ``holidays`` package enriches statutory holiday names when it
    is installed. Built-in solar and lunar tables remain the deterministic
    fallback so cultural dates such as Qixi work without extra dependencies.

    Args:
        date_obj: Solar date to inspect.

    Returns:
        A de-duplicated Chinese holiday name string, or an empty string.
    """
    names: list[str] = []
    try:
        import holidays as holiday_library

        name = holiday_library.CN().get(date_obj)
        if name:
            names.extend(str(name).replace(",", "、").split("、"))
    except ModuleNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[BusySchedule] Optional holiday lookup failed: {exc}")

    solar_name = _SOLAR_HOLIDAYS.get((date_obj.month, date_obj.day))
    if solar_name:
        names.append(solar_name)

    lunar = _solar_to_lunar(date_obj)
    if lunar:
        _year, month, day, is_leap = lunar
        if not is_leap:
            lunar_name = _LUNAR_HOLIDAYS.get((month, day))
            if lunar_name:
                names.append(lunar_name)

    cleaned: list[str] = []
    seen: set[str] = set()
    for name in names:
        item = str(name or "").strip()
        if item and item not in seen:
            seen.add(item)
            cleaned.append(item)
    return "、".join(cleaned)


def get_weekday_cn(date_obj: date) -> str:
    """Return the Chinese weekday name.

    Args:
        date_obj: Solar date to inspect.

    Returns:
        Chinese weekday text.
    """
    return _WEEKDAY_CN[date_obj.weekday()]


def _parse_special_day_rules(config: object, date_obj: date) -> list[str]:
    raw_rules = _cfg_lookup(config, "special_day_rules", [])
    if not isinstance(raw_rules, list):
        return []

    matched: list[tuple[int, int, str]] = []
    for index, item in enumerate(raw_rules):
        if not isinstance(item, dict):
            continue
        if item.get("enabled", True) is False:
            continue

        exact_date = None
        exact_text = str(item.get("date") or item.get("special_date") or "").strip()
        if exact_text:
            try:
                exact_date = date.fromisoformat(exact_text)
            except ValueError:
                logger.warning(
                    "[BusySchedule] Ignoring special day rule %s: invalid date %r",
                    index,
                    exact_text,
                )
                exact_date = None

        year = _safe_int(item.get("year"))
        month = _safe_int(item.get("month"))
        day = _safe_int(item.get("day"))
        year = year or None
        month = month or None
        day = day or None
        weekday = _normalize_weekday(item.get("weekday") or item.get("week_day"))
        if month is not None and not 1 <= month <= 12:
            logger.warning(
                "[BusySchedule] Ignoring special day rule %s: invalid month", index
            )
            continue
        if day is not None and not 1 <= day <= 31:
            logger.warning(
                "[BusySchedule] Ignoring special day rule %s: invalid day", index
            )
            continue
        if weekday is None and item.get("weekday") not in (None, ""):
            logger.warning(
                "[BusySchedule] Ignoring special day rule %s: invalid weekday",
                index,
            )
            continue

        has_condition = any(
            value is not None for value in (exact_date, year, month, day, weekday)
        )
        if not has_condition:
            logger.warning(
                "[BusySchedule] Ignoring special day rule %s: no date condition", index
            )
            continue

        if exact_date and date_obj != exact_date:
            continue
        if year is not None and date_obj.year != year:
            continue
        if month is not None and date_obj.month != month:
            continue
        if day is not None and date_obj.day != day:
            continue
        if weekday is not None and date_obj.weekday() != weekday:
            continue

        title = str(
            item.get("title") or item.get("name") or item.get("label") or ""
        ).strip()
        note = str(item.get("note") or "").strip()
        priority = _safe_int(item.get("priority"), 0) or 0

        if title:
            label = title if not note else f"{title}（{note}）"
        elif exact_date is not None:
            label = exact_date.isoformat()
        elif month is not None and day is not None:
            label = f"{month}月{day}日"
        elif month is None and day is not None and weekday is None:
            label = f"每月{day}日"
        elif weekday is not None:
            label = f"每周{_WEEKDAY_SHORT[weekday]}"
        else:
            label = note or "特别日子"

        matched.append((priority, index, label))

    matched.sort(key=lambda item: (-item[0], item[1]))
    labels: list[str] = []
    seen: set[str] = set()
    for _priority, _index, label in matched:
        if label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def build_calendar_context(
    date_obj: date, config: object | None = None
) -> dict[str, str]:
    """Build a reusable date context for prompt generation and injection.

    Args:
        date_obj: Date represented by the context.
        config: Plugin configuration containing optional custom rules.

    Returns:
        String values suitable for ``str.format`` and prompt injection.
    """
    holiday_name = get_holiday(date_obj)
    holiday = holiday_name or "无已知节日"
    lunar_date = get_lunar_date_cn(date_obj)
    special_days = _parse_special_day_rules(config, date_obj)
    special_day = (
        holiday_name or (special_days[0] if special_days else "") or "无特别日"
    )
    summary_parts = [date_obj.strftime("%Y年%m月%d日"), get_weekday_cn(date_obj)]
    if lunar_date:
        summary_parts.append(lunar_date)
    if holiday_name:
        summary_parts.append(holiday_name)
    if special_days:
        summary_parts.append("；".join(special_days))

    return {
        "date_str": date_obj.strftime("%Y年%m月%d日"),
        "weekday": get_weekday_cn(date_obj),
        "holiday": holiday,
        "lunar_date": lunar_date,
        "special_day": special_day,
        "special_days": "\n".join(f"- {item}" for item in special_days)
        or "无自定义特别日",
        "today_summary": "，".join(summary_parts),
    }
