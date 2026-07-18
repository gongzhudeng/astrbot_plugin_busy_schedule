"""Weather forecast providers and cycle-scoped cache."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from astrbot.api import logger

_WMO_TEXT = {
    0: "晴",
    1: "大部晴朗",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "强毛毛雨",
    56: "轻微冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "轻微冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "小阵雨",
    81: "中阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷雨",
    96: "雷雨伴小冰雹",
    99: "雷雨伴强冰雹",
}


@dataclass(frozen=True)
class WeatherHour:
    """Normalized hourly forecast."""

    time: str
    temperature_c: float | None
    condition: str
    precipitation_mm: float = 0.0
    precipitation_probability: int | None = None
    wind_direction: str = ""
    wind_speed_kmh: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WeatherHour:
        return cls(**data)


@dataclass
class WeatherSnapshot:
    """Normalized weather data bound to one schedule cycle."""

    location_key: str
    configured_city: str
    display_location: str
    timezone: str
    provider: str
    fetched_at: str
    cycle_start: str
    cycle_end: str
    temperature_min_c: float
    temperature_max_c: float
    hours: list[WeatherHour] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WeatherSnapshot:
        payload = dict(data)
        payload["hours"] = [
            item if isinstance(item, WeatherHour) else WeatherHour.from_dict(item)
            for item in payload.get("hours", [])
        ]
        return cls(**payload)

    def covers(
        self, location_key: str, cycle_start: datetime, cycle_end: datetime
    ) -> bool:
        if self.location_key != location_key or not self.hours:
            return False
        try:
            stored_start = datetime.fromisoformat(self.cycle_start)
            stored_end = datetime.fromisoformat(self.cycle_end)
            first_hour = datetime.fromisoformat(self.hours[0].time)
            last_hour = datetime.fromisoformat(self.hours[-1].time)
        except (TypeError, ValueError):
            return False
        return (
            stored_start == cycle_start
            and stored_end == cycle_end
            and first_hour <= cycle_start
            and last_hour + timedelta(hours=1) >= cycle_end
        )

    def format_for_prompt(self) -> str:
        """Format a compact forecast without losing dry/wet transitions."""
        cycle_start = datetime.fromisoformat(self.cycle_start)
        lines = [
            f"地点：{self.display_location}（时区 {self.timezone}）",
            f"周期温度：{self.temperature_min_c:g}~{self.temperature_max_c:g}℃",
            "天气时段：",
        ]
        for segment in _weather_segments(self.hours):
            start = datetime.fromisoformat(segment[0].time)
            end = datetime.fromisoformat(segment[-1].time) + timedelta(hours=1)
            temperatures = [
                hour.temperature_c for hour in segment if hour.temperature_c is not None
            ]
            temperature = _temperature_range(temperatures)
            label = _segment_weather_label(segment)
            risk = "，降雨风险高" if _weather_state(segment[0])[2] else ""
            lines.append(
                f"- {_format_time_range(start, end, cycle_start.date())} "
                f"{label}，{temperature}{risk}"
            )

        risks = _format_weather_risks(self.hours, cycle_start.date())
        if risks:
            lines.append("出行风险：")
            lines.extend(f"- {risk}" for risk in risks)
        return "\n".join(lines)

    def format_summary(self) -> str:
        detail = self.summary or "无明显降水时段"
        return (
            f"{self.display_location}：{self.temperature_min_c:g}~"
            f"{self.temperature_max_c:g}℃；{detail}；"
            f"来源 {self.provider}，更新于 "
            f"{datetime.fromisoformat(self.fetched_at).strftime('%m-%d %H:%M')}"
        )


ConfigGetter = Callable[[str, Any], Any]


class WeatherService:
    """Fetch weather through an ordered provider chain and persist snapshots."""

    def __init__(self, config_getter: ConfigGetter, cache_file: Path):
        self._cfg = config_getter
        self.cache_file = cache_file

    def _location_key(self) -> str:
        parts = (
            str(self._cfg("weather_city", "")).strip().casefold(),
            str(self._cfg("weather_admin", "")).strip().casefold(),
            str(self._cfg("weather_country_code", "")).strip().upper(),
        )
        return "|".join(parts)

    def _cycle_bounds(
        self, owner_date: date, schedule_time: tuple[int, int]
    ) -> tuple[datetime, datetime]:
        start = datetime.combine(owner_date, datetime.min.time()).replace(
            hour=schedule_time[0], minute=schedule_time[1]
        )
        return start, start + timedelta(days=1)

    def _load_cache(self) -> list[WeatherSnapshot]:
        if not self.cache_file.exists():
            return []
        try:
            raw = json.loads(self.cache_file.read_text(encoding="utf-8-sig"))
            items = raw if isinstance(raw, list) else []
            return [WeatherSnapshot.from_dict(item) for item in items]
        except Exception as exc:
            logger.warning(f"[BusySchedule] Failed to load weather cache: {exc}")
            return []

    def _save_cache(self, snapshot: WeatherSnapshot) -> None:
        try:
            existing = [
                item
                for item in self._load_cache()
                if not (
                    item.location_key == snapshot.location_key
                    and item.cycle_start == snapshot.cycle_start
                )
            ]
            existing.append(snapshot)
            existing.sort(key=lambda item: item.cycle_start, reverse=True)
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(
                json.dumps(
                    [item.to_dict() for item in existing[:14]],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"[BusySchedule] Failed to save weather cache: {exc}")

    def get_cached(
        self, owner_date: date, schedule_time: tuple[int, int]
    ) -> WeatherSnapshot | None:
        location_key = self._location_key()
        cycle_start, cycle_end = self._cycle_bounds(owner_date, schedule_time)
        return next(
            (
                item
                for item in self._load_cache()
                if item.covers(location_key, cycle_start, cycle_end)
            ),
            None,
        )

    async def get_forecast(
        self, owner_date: date, schedule_time: tuple[int, int]
    ) -> WeatherSnapshot | None:
        """Return a cached or freshly fetched forecast for schedule generation."""
        snapshot, _ = await self.query_forecast(
            owner_date, schedule_time, force_refresh=False
        )
        return snapshot

    async def query_forecast(
        self,
        owner_date: date,
        schedule_time: tuple[int, int],
        *,
        force_refresh: bool,
    ) -> tuple[WeatherSnapshot | None, tuple[str, ...]]:
        """Fetch a forecast and return sanitized provider diagnostics."""
        if not bool(self._cfg("weather_enabled", False)):
            return None, ("天气服务未启用（weather_enabled=false）",)
        city = str(self._cfg("weather_city", "")).strip()
        if not city:
            logger.warning("[BusySchedule] Weather enabled but weather_city is empty")
            return None, ("固定城市未配置（weather_city 为空）",)

        if not force_refresh:
            cached = self.get_cached(owner_date, schedule_time)
            if cached:
                logger.info(
                    f"[BusySchedule] Reusing weather cache for {cached.display_location}"
                )
                return cached, ()

        providers = self._cfg("weather_providers", ["qweather", "open_meteo"])
        if isinstance(providers, str):
            providers = [part.strip() for part in providers.split(",")]
        cycle_start, cycle_end = self._cycle_bounds(owner_date, schedule_time)
        seen: set[str] = set()
        errors = []
        fetchers = {
            "open_meteo": self._fetch_open_meteo,
            "qweather": self._fetch_qweather,
        }
        for raw_name in providers or []:
            name = str(raw_name).strip().lower().replace("-", "_")
            if not name or name in seen:
                continue
            seen.add(name)
            fetcher = fetchers.get(name)
            if not fetcher:
                message = f"{name}: 不支持的天气供应商"
                errors.append(message)
                logger.warning(f"[BusySchedule] {message}")
                continue
            try:
                snapshot = await fetcher(cycle_start, cycle_end)
                self._save_cache(snapshot)
                logger.info(
                    f"[BusySchedule] Weather fetched from {name}: "
                    f"{snapshot.display_location}, hours={len(snapshot.hours)}"
                )
                return snapshot, tuple(errors)
            except Exception as exc:
                detail = self._sanitize_error(exc)
                errors.append(f"{name}: {type(exc).__name__}: {detail}")
                logger.warning(
                    f"[BusySchedule] Weather provider {name} failed: {detail}"
                )

        if not seen:
            errors.append("未配置有效的天气供应商")
        logger.warning(
            "[BusySchedule] All weather providers failed; generating without weather"
        )
        return None, tuple(errors)

    def _sanitize_error(self, error: Exception) -> str:
        message = str(error) or "未知错误"
        sensitive_keys = ("qweather_api_key", "qweather_proxy", "open_meteo_proxy")
        for key in sensitive_keys:
            value = str(self._cfg(key, "")).strip()
            if value:
                message = message.replace(value, "[redacted]")
        return message

    def _client(self, proxy_key: str) -> httpx.AsyncClient:
        timeout = max(1.0, float(self._cfg("weather_timeout_seconds", 10)))
        proxy = str(self._cfg(proxy_key, "")).strip() or None
        return httpx.AsyncClient(timeout=timeout, proxy=proxy, follow_redirects=True)

    def _location_params(self) -> tuple[str, str, str]:
        return (
            str(self._cfg("weather_city", "")).strip(),
            str(self._cfg("weather_admin", "")).strip(),
            str(self._cfg("weather_country_code", "")).strip().upper(),
        )

    @staticmethod
    def _select_location(
        locations: list[dict[str, Any]], admin: str, country_code: str
    ) -> dict[str, Any]:
        if not locations:
            raise ValueError("city was not found")
        admin_folded = admin.casefold()
        for item in locations:
            item_country = str(
                item.get("country_code") or item.get("country") or ""
            ).upper()
            item_admin = " ".join(
                str(item.get(key, "")) for key in ("admin1", "admin2", "adm1", "adm2")
            ).casefold()
            if country_code and item_country != country_code:
                continue
            if admin_folded and admin_folded not in item_admin:
                continue
            return item
        raise ValueError("no city result matched weather_admin/weather_country_code")

    async def _fetch_open_meteo(
        self, cycle_start: datetime, cycle_end: datetime
    ) -> WeatherSnapshot:
        city, admin, country_code = self._location_params()
        async with self._client("open_meteo_proxy") as client:
            geo_response = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 10, "language": "zh", "format": "json"},
            )
            geo_response.raise_for_status()
            location = self._select_location(
                geo_response.json().get("results", []), admin, country_code
            )
            forecast_response = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": location["latitude"],
                    "longitude": location["longitude"],
                    "timezone": "auto",
                    "start_date": cycle_start.date().isoformat(),
                    "end_date": cycle_end.date().isoformat(),
                    "hourly": (
                        "temperature_2m,precipitation,precipitation_probability,"
                        "weather_code,wind_speed_10m,wind_direction_10m"
                    ),
                    "daily": "temperature_2m_max,temperature_2m_min",
                },
            )
            forecast_response.raise_for_status()
            payload = forecast_response.json()

        hourly = payload.get("hourly", {})
        times = hourly.get("time", [])
        hours = []
        for index, raw_time in enumerate(times):
            moment = datetime.fromisoformat(raw_time)
            if not _hour_intersects_cycle(moment, cycle_start, cycle_end):
                continue
            wind_degrees = _number_at(hourly, "wind_direction_10m", index)
            hours.append(
                WeatherHour(
                    time=moment.isoformat(timespec="minutes"),
                    temperature_c=_number_at(hourly, "temperature_2m", index),
                    condition=_WMO_TEXT.get(
                        int(_number_at(hourly, "weather_code", index)), "未知天气"
                    ),
                    precipitation_mm=_number_at(hourly, "precipitation", index),
                    precipitation_probability=_optional_int_at(
                        hourly, "precipitation_probability", index
                    ),
                    wind_direction=_wind_direction(wind_degrees),
                    wind_speed_kmh=_number_at(hourly, "wind_speed_10m", index),
                )
            )
        daily = payload.get("daily", {})
        return self._snapshot(
            provider="open_meteo",
            city=city,
            display_location=_display_location(location),
            timezone=str(payload.get("timezone", location.get("timezone", ""))),
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            hours=hours,
            daily_min=_first_daily_value(daily, "temperature_2m_min"),
            daily_max=_first_daily_value(daily, "temperature_2m_max"),
        )

    async def _fetch_qweather(
        self, cycle_start: datetime, cycle_end: datetime
    ) -> WeatherSnapshot:
        host = str(self._cfg("qweather_api_host", "")).strip().rstrip("/")
        api_key = str(self._cfg("qweather_api_key", "")).strip()
        if not host or not api_key:
            raise ValueError("QWeather API Host or API KEY is not configured")
        if not host.startswith("https://"):
            host = f"https://{host}"
        city, admin, country_code = self._location_params()
        headers = {"X-QW-Api-Key": api_key, "Accept-Encoding": "gzip"}
        async with self._client("qweather_proxy") as client:
            geo_response = await client.get(
                f"{host}/geo/v2/city/lookup",
                params={
                    "location": city,
                    "adm": admin or None,
                    "range": country_code.lower() or None,
                    "number": 10,
                    "lang": "zh",
                },
                headers=headers,
            )
            geo_response.raise_for_status()
            geo_payload = geo_response.json()
            _require_qweather_ok(geo_payload)
            location = self._select_location(geo_payload.get("location", []), admin, "")
            location_id = location["id"]
            hourly_response = await client.get(
                f"{host}/v7/weather/72h",
                params={"location": location_id, "lang": "zh"},
                headers=headers,
            )
            daily_response = await client.get(
                f"{host}/v7/weather/3d",
                params={"location": location_id, "lang": "zh"},
                headers=headers,
            )
            hourly_response.raise_for_status()
            daily_response.raise_for_status()
            hourly_payload = hourly_response.json()
            daily_payload = daily_response.json()
            _require_qweather_ok(hourly_payload)
            _require_qweather_ok(daily_payload)

        hours = []
        for item in hourly_payload.get("hourly", []):
            moment = datetime.fromisoformat(item["fxTime"]).replace(tzinfo=None)
            if not _hour_intersects_cycle(moment, cycle_start, cycle_end):
                continue
            hours.append(
                WeatherHour(
                    time=moment.isoformat(timespec="minutes"),
                    temperature_c=float(item["temp"]),
                    condition=str(item.get("text", "未知天气")),
                    precipitation_mm=float(item.get("precip") or 0),
                    precipitation_probability=_optional_int(item.get("pop")),
                    wind_direction=str(item.get("windDir", "")),
                    wind_speed_kmh=_optional_float(item.get("windSpeed")),
                )
            )
        daily_items = daily_payload.get("daily", [])
        first_daily = daily_items[0] if daily_items else {}
        return self._snapshot(
            provider="qweather",
            city=city,
            display_location=_display_location(location),
            timezone=str(location.get("tz", "")),
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            hours=hours,
            daily_min=_optional_float(first_daily.get("tempMin")),
            daily_max=_optional_float(first_daily.get("tempMax")),
        )

    def _snapshot(
        self,
        *,
        provider: str,
        city: str,
        display_location: str,
        timezone: str,
        cycle_start: datetime,
        cycle_end: datetime,
        hours: list[WeatherHour],
        daily_min: float | None = None,
        daily_max: float | None = None,
    ) -> WeatherSnapshot:
        hours = _complete_cycle_hours(hours, cycle_start, cycle_end)
        actual_hours = [hour for hour in hours if hour.temperature_c is not None]
        if len(actual_hours) < 18:
            raise ValueError(
                "forecast has too many missing hours for the schedule cycle: "
                f"{len(actual_hours)}/24"
            )
        temperatures = [
            hour.temperature_c
            for hour in actual_hours
            if hour.temperature_c is not None
        ]
        return WeatherSnapshot(
            location_key=self._location_key(),
            configured_city=city,
            display_location=display_location,
            timezone=timezone,
            provider=provider,
            fetched_at=datetime.now().isoformat(timespec="seconds"),
            cycle_start=cycle_start.isoformat(timespec="minutes"),
            cycle_end=cycle_end.isoformat(timespec="minutes"),
            temperature_min_c=(
                daily_min if daily_min is not None else min(temperatures)
            ),
            temperature_max_c=(
                daily_max if daily_max is not None else max(temperatures)
            ),
            hours=hours,
            summary=_precipitation_summary(hours),
        )


def _hour_intersects_cycle(
    hour_start: datetime, cycle_start: datetime, cycle_end: datetime
) -> bool:
    return hour_start < cycle_end and hour_start + timedelta(hours=1) > cycle_start


def _complete_cycle_hours(
    hours: list[WeatherHour], cycle_start: datetime, cycle_end: datetime
) -> list[WeatherHour]:
    by_time = {datetime.fromisoformat(hour.time): hour for hour in hours}
    completed = []
    moment = cycle_start.replace(minute=0, second=0, microsecond=0)
    while moment < cycle_end:
        completed.append(
            by_time.get(
                moment,
                WeatherHour(
                    time=moment.isoformat(timespec="minutes"),
                    temperature_c=None,
                    condition="预报暂缺",
                ),
            )
        )
        moment += timedelta(hours=1)
    return completed


def _first_daily_value(payload: dict[str, Any], key: str) -> float | None:
    values = payload.get(key, [])
    return _optional_float(values[0]) if values else None


def _number_at(payload: dict[str, Any], key: str, index: int) -> float:
    values = payload.get(key, [])
    if index >= len(values) or values[index] is None:
        return 0.0
    return float(values[index])


def _optional_int_at(payload: dict[str, Any], key: str, index: int) -> int | None:
    values = payload.get(key, [])
    return _optional_int(values[index]) if index < len(values) else None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(float(value))


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _wind_direction(degrees: float) -> str:
    names = ("北风", "东北风", "东风", "东南风", "南风", "西南风", "西风", "西北风")
    return names[int((degrees + 22.5) % 360 // 45)]


def _display_location(location: dict[str, Any]) -> str:
    parts = [
        str(location.get("name", "")).strip(),
        str(location.get("admin1") or location.get("adm1") or "").strip(),
        str(location.get("country", "")).strip(),
    ]
    return "，".join(dict.fromkeys(part for part in parts if part))


def _require_qweather_ok(payload: dict[str, Any]) -> None:
    if str(payload.get("code")) != "200":
        raise ValueError(f"QWeather returned code {payload.get('code', 'unknown')}")


def _weather_state(hour: WeatherHour) -> tuple[str, str, bool]:
    """Project one hour into a truthful prompt-level weather state."""
    condition = hour.condition.strip() or "未知天气"
    high_rain_risk = (
        hour.precipitation_mm <= 0 and (hour.precipitation_probability or 0) >= 50
    )
    if condition == "预报暂缺" or hour.temperature_c is None:
        return "missing", "预报暂缺", False
    if "雷" in condition:
        return "thunderstorm", condition, False
    if "冰雹" in condition:
        return "hail", condition, False
    if "冻雨" in condition or "冻毛毛雨" in condition:
        return "freezing_rain", condition, False
    if "雪" in condition:
        return "snow", condition, False
    if "雨" in condition or hour.precipitation_mm > 0:
        if any(token in condition for token in ("大雨", "强阵雨", "暴雨")):
            return "heavy_rain", condition, False
        if any(token in condition for token in ("中雨", "中阵雨")):
            return "moderate_rain", condition, False
        return "light_rain", condition, False
    if "雾" in condition:
        return "fog", condition, high_rain_risk
    return "dry", condition, high_rain_risk


def _weather_segments(hours: list[WeatherHour]) -> list[list[WeatherHour]]:
    """Merge adjacent hours by semantic state without crossing dry/wet boundaries."""
    segments: list[list[WeatherHour]] = []
    for hour in hours:
        state, _, risk = _weather_state(hour)
        previous_key = None
        if segments:
            previous_state, _, previous_risk = _weather_state(segments[-1][-1])
            previous_key = (previous_state, previous_risk)
        if previous_key != (state, risk):
            segments.append([hour])
        else:
            segments[-1].append(hour)
    return segments


def _segment_weather_label(segment: list[WeatherHour]) -> str:
    labels = list(dict.fromkeys(_weather_state(hour)[1] for hour in segment))
    return "转".join(labels)


def _temperature_range(temperatures: list[float]) -> str:
    if not temperatures:
        return "温度未知"
    low, high = min(temperatures), max(temperatures)
    if low == high:
        return f"约{low:g}℃"
    return f"{low:g}~{high:g}℃"


def _format_time_range(start: datetime, end: datetime, base_date: date) -> str:
    start_text = start.strftime("%H:%M")
    end_text = end.strftime("%H:%M")
    if start.date() != base_date:
        start_text = start.strftime("%m-%d %H:%M")
    if end.date() not in (start.date(), base_date):
        end_text = end.strftime("%m-%d %H:%M")
    return f"{start_text}-{end_text}"


def _matching_ranges(
    hours: list[WeatherHour],
    predicate: Callable[[WeatherHour], bool],
) -> list[list[WeatherHour]]:
    groups: list[list[WeatherHour]] = []
    for hour in hours:
        if not predicate(hour):
            continue
        moment = datetime.fromisoformat(hour.time)
        if groups:
            previous = datetime.fromisoformat(groups[-1][-1].time)
            if moment - previous == timedelta(hours=1):
                groups[-1].append(hour)
                continue
        groups.append([hour])
    return groups


def _risk_ranges(
    hours: list[WeatherHour],
    predicate: Callable[[WeatherHour], bool],
    base_date: date,
) -> str:
    ranges = []
    for group in _matching_ranges(hours, predicate):
        start = datetime.fromisoformat(group[0].time)
        end = datetime.fromisoformat(group[-1].time) + timedelta(hours=1)
        ranges.append(_format_time_range(start, end, base_date))
    return "、".join(ranges)


def _format_weather_risks(hours: list[WeatherHour], base_date: date) -> list[str]:
    """Keep impactful raw signals while hiding routine hourly measurements."""
    risks = []
    rules: tuple[tuple[str, Callable[[WeatherHour], bool]], ...] = (
        (
            "雷暴/冰雹/冻雨风险",
            lambda hour: any(
                token in hour.condition for token in ("雷", "冰雹", "冻雨", "冻毛毛雨")
            ),
        ),
        (
            "强降水风险",
            lambda hour: (
                hour.precipitation_mm >= 10
                or any(token in hour.condition for token in ("暴雨", "大雨", "强阵雨"))
            ),
        ),
        (
            "强风风险",
            lambda hour: (hour.wind_speed_kmh or 0) >= 39,
        ),
        (
            "高温风险",
            lambda hour: hour.temperature_c is not None and hour.temperature_c >= 35,
        ),
        (
            "低温/结冰风险",
            lambda hour: hour.temperature_c is not None and hour.temperature_c <= 0,
        ),
    )
    for label, predicate in rules:
        ranges = _risk_ranges(hours, predicate, base_date)
        if ranges:
            risks.append(f"{ranges} {label}")
    return risks


def _precipitation_summary(hours: list[WeatherHour]) -> str:
    wet = [
        hour
        for hour in hours
        if hour.precipitation_mm > 0 or "雨" in hour.condition or "雪" in hour.condition
    ]
    if not wet:
        return "无明显降水时段"
    groups: list[list[WeatherHour]] = []
    for hour in wet:
        moment = datetime.fromisoformat(hour.time)
        if not groups:
            groups.append([hour])
            continue
        previous = datetime.fromisoformat(groups[-1][-1].time)
        if moment - previous <= timedelta(hours=1):
            groups[-1].append(hour)
        else:
            groups.append([hour])
    ranges = []
    for group in groups[:3]:
        start = datetime.fromisoformat(group[0].time)
        end = datetime.fromisoformat(group[-1].time) + timedelta(hours=1)
        ranges.append(f"{start.strftime('%m-%d %H:%M')}~{end.strftime('%H:%M')}")
    return "主要降水时段 " + "、".join(ranges)
