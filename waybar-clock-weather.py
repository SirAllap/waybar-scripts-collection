#!/usr/bin/env python3
"""
Waybar Clock + Weather Module

Merges calendar/clock and weather into a single module.
Bar:     HH:MM  Fri, Feb 21  │  ⛅ 18°C
Tooltip: weather forecast → calendar grid → moon phase → system info
"""

from __future__ import annotations

import calendar as cal_mod
import html
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Final, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import tomllib
except ImportError:
    tomllib = None  # type: ignore


# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    ICON_CLOCK:    Final[str] = ""
    ICON_CALENDAR: Final[str] = ""
    ICON_MOON:     Final[str] = "󰽢"
    ICON_UPTIME:   Final[str] = "󰔚"

    THEME_PATH:  Final[Path] = Path.home() / ".config/omarchy/current/theme/colors.toml"
    CACHE_FILE:  Final[Path] = Path.home() / ".cache/waybar_weather/data.json"
    CACHE_TTL:   Final[int]  = 900    # 15 min
    API_TIMEOUT: Final[int]  = 10

    WEATHER_LAT:  Final[float] = float(os.environ.get("WAYBAR_WEATHER_LAT", "0.0"))
    WEATHER_LON:  Final[float] = float(os.environ.get("WAYBAR_WEATHER_LON", "0.0"))
    WEATHER_CITY: Final[str]   = os.environ.get("WAYBAR_WEATHER_CITY", "My City")

    SEPARATOR_WIDTH: Final[int] = 56
    CALENDAR_WIDTH:  Final[int] = 34

    LUNAR_CYCLE_DAYS:   Final[float]    = 29.53058867
    NEW_MOON_REFERENCE: Final[datetime] = datetime(2000, 1, 6, 18, 14)
    FULL_MOON_OFFSET:   Final[float]    = 14.765


# ============================================================================
# THEME
# ============================================================================

@dataclass(frozen=True)
class ThemeColors:
    black:          str = "#1e1e2e"
    red:            str = "#f38ba8"
    green:          str = "#a6e3a1"
    yellow:         str = "#f9e2af"
    blue:           str = "#89b4fa"
    magenta:        str = "#f5c2e7"
    cyan:           str = "#89dceb"
    white:          str = "#cdd6f4"
    bright_black:   str = "#6c7086"
    bright_red:     str = "#f38ba8"
    bright_green:   str = "#a6e3a1"
    bright_yellow:  str = "#f9e2af"
    bright_blue:    str = "#89b4fa"
    bright_magenta: str = "#f5c2e7"
    bright_cyan:    str = "#94e2d5"
    bright_white:   str = "#bac2de"
    orange:         str = "#ef9f76"

    @classmethod
    def from_omarchy(cls, path: Path) -> ThemeColors:
        if not tomllib or not path.exists():
            return cls()
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            d = cls()
            return cls(
                black=data.get("color0",  d.black),
                red=data.get("color1",    d.red),
                green=data.get("color2",  d.green),
                yellow=data.get("color3", d.yellow),
                blue=data.get("color4",   d.blue),
                magenta=data.get("color5",d.magenta),
                cyan=data.get("color6",   d.cyan),
                white=data.get("color7",  d.white),
                bright_black=data.get("color8",    d.bright_black),
                bright_red=data.get("color9",      d.bright_red),
                bright_green=data.get("color10",   d.bright_green),
                bright_yellow=data.get("color11",  d.bright_yellow),
                bright_blue=data.get("color12",    d.bright_blue),
                bright_magenta=data.get("color13", d.bright_magenta),
                bright_cyan=data.get("color14",    d.bright_cyan),
                bright_white=data.get("color15",   d.bright_white),
                orange=d.orange,
            )
        except Exception:
            return cls()


THEME: Final = ThemeColors.from_omarchy(Config.THEME_PATH)


# ============================================================================
# WEATHER MODELS
# ============================================================================

class SeverityLevel(Enum):
    LOW          = ("green",  1)
    MODERATE     = ("yellow", 2)
    HIGH         = ("orange", 3)
    VERY_HIGH    = ("red",    4)
    EXTREME      = ("magenta",5)
    CATASTROPHIC = ("magenta",6)

    def __init__(self, color_key: str, rank: int) -> None:
        self.color_key = color_key
        self.rank = rank


@dataclass(frozen=True)
class WeatherCondition:
    code: int
    icon: str
    description: str

    @classmethod
    def from_code(cls, code: int) -> WeatherCondition:
        return WEATHER_MAP.get(code, cls(code, "❓", "Unknown"))


@dataclass(frozen=True)
class WindInfo:
    speed_kph: float
    direction_deg: float

    @property
    def direction(self) -> str:
        idx = int((self.direction_deg % 360 + 11.25) / 22.5) % 16
        return WIND_DIRECTIONS[idx]

    @property
    def arrow(self) -> str:
        return WIND_ARROWS.get(self.direction, "○")

    @property
    def severity(self) -> SeverityLevel:
        for threshold, level in [
            (10, SeverityLevel.LOW), (20, SeverityLevel.LOW),
            (30, SeverityLevel.MODERATE), (40, SeverityLevel.MODERATE),
            (50, SeverityLevel.HIGH), (63, SeverityLevel.HIGH),
            (75, SeverityLevel.VERY_HIGH), (89, SeverityLevel.VERY_HIGH),
            (103, SeverityLevel.EXTREME),
        ]:
            if self.speed_kph < threshold:
                return level
        return SeverityLevel.CATASTROPHIC


@dataclass(frozen=True)
class CurrentWeather:
    temp: float
    feels_like: float
    humidity: int
    wind: WindInfo
    uv_index: float
    condition: WeatherCondition
    precipitation: float

    @property
    def fire_danger(self) -> tuple[str, SeverityLevel]:
        if self.humidity > 70:
            return ("Low-Moderate", SeverityLevel.LOW)
        score = (self.temp * 0.5) + (self.wind.speed_kph * 0.8) - (self.humidity * 0.5)
        for threshold, level in [
            (12, SeverityLevel.LOW), (24, SeverityLevel.HIGH),
            (38, SeverityLevel.VERY_HIGH), (50, SeverityLevel.EXTREME),
        ]:
            if score < threshold:
                return (level.name.replace("_", " ").title(), level)
        return ("Catastrophic", SeverityLevel.CATASTROPHIC)


# ============================================================================
# CONSTANTS
# ============================================================================

WEATHER_MAP: Final[dict[int, WeatherCondition]] = {
    0:  WeatherCondition(0, "", "Clear sky"),
    1:  WeatherCondition(1, "", "Mainly clear"),
    2:  WeatherCondition(2, "", "Partly cloudy"),
    3:  WeatherCondition(3, "", "Overcast"),
    45: WeatherCondition(45, "", "Fog"),
    48: WeatherCondition(48, "", "Depositing rime fog"),
    51: WeatherCondition(51, "", "Light drizzle"),
    53: WeatherCondition(53, "", "Moderate drizzle"),
    55: WeatherCondition(55, "", "Dense drizzle"),
    56: WeatherCondition(56, "", "Light freezing drizzle"),
    57: WeatherCondition(57, "", "Dense freezing drizzle"),
    61: WeatherCondition(61, "", "Slight rain"),
    63: WeatherCondition(63, "", "Moderate rain"),
    65: WeatherCondition(65, "", "Heavy rain"),
    66: WeatherCondition(66, "", "Light freezing rain"),
    67: WeatherCondition(67, "", "Heavy freezing rain"),
    71: WeatherCondition(71, "", "Slight snowfall"),
    73: WeatherCondition(73, "", "Moderate snowfall"),
    75: WeatherCondition(75, "", "Heavy snowfall"),
    77: WeatherCondition(77, "", "Snow grains"),
    80: WeatherCondition(80, "", "Slight rain showers"),
    81: WeatherCondition(81, "", "Moderate rain showers"),
    82: WeatherCondition(82, "", "Violent rain showers"),
    85: WeatherCondition(85, "", "Slight snow showers"),
    86: WeatherCondition(86, "", "Heavy snow showers"),
    95: WeatherCondition(95, "", "Thunderstorm"),
    96: WeatherCondition(96, "", "Thunderstorm with hail"),
    99: WeatherCondition(99, "", "Thunderstorm with heavy hail"),
}

WIND_DIRECTIONS: Final[list[str]] = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]

WIND_ARROWS: Final[dict[str, str]] = {
    "N": "↑",  "NNE": "↗", "NE": "↗",  "ENE": "↗",
    "E": "→",  "ESE": "↘", "SE": "↘",  "SSE": "↘",
    "S": "↓",  "SSW": "↙", "SW": "↙",  "WSW": "↙",
    "W": "←",  "WNW": "↖", "NW": "↖",  "NNW": "↖",
}

CLOCK_ICONS: Final[list[str]] = [
    "󱑊", "󱐿", "󱑀", "󱑁", "󱑂", "󱑃",
    "󱑄", "󱑅", "󱑆", "󱑇", "󱑈", "󱑉",
]

UV_THRESHOLDS: Final = [
    (3,  "Low",       SeverityLevel.LOW),
    (6,  "Moderate",  SeverityLevel.MODERATE),
    (8,  "High",      SeverityLevel.HIGH),
    (11, "Very High", SeverityLevel.VERY_HIGH),
]

HUMIDITY_LEVELS: Final = [
    (20, " Extreme Dry ",      SeverityLevel.EXTREME),
    (30, "⚡ Very Dry ⚡",      SeverityLevel.VERY_HIGH),
    (40, " Pleasant ",         SeverityLevel.HIGH),
    (50, " Perfect ",          SeverityLevel.LOW),
    (60, " Little Bit Humid ", SeverityLevel.MODERATE),
    (70, " Getting Sticky ",   SeverityLevel.MODERATE),
    (80, " Properly Humid Now ",SeverityLevel.MODERATE),
    (90, " Tropical Sauna Mode ",SeverityLevel.HIGH),
]


# ============================================================================
# COLOR HELPERS
# ============================================================================

def sev_color(level: SeverityLevel) -> str:
    return getattr(THEME, level.color_key, THEME.white)


def temp_color(temp: float) -> str:
    for max_t, color in [
        (15, THEME.blue), (18, THEME.blue), (21, THEME.cyan), (24, THEME.cyan),
        (27, THEME.green), (30, THEME.yellow), (32, THEME.yellow), (100, THEME.red),
    ]:
        if temp <= max_t:
            return color
    return THEME.red


def fmt_temp(t: float) -> str:
    return f"<span foreground='{temp_color(t)}'>{t:.1f}°C</span>"


def fmt_sev(value: Any, level: SeverityLevel, suffix: str = "") -> str:
    return f"<span foreground='{sev_color(level)}'>{value}{suffix}</span>"


def get_uv_info(uv: float) -> tuple[str, SeverityLevel]:
    for threshold, desc, level in UV_THRESHOLDS:
        if uv < threshold:
            return (desc, level)
    return ("Extreme", SeverityLevel.EXTREME)


def get_humidity_info(h: int) -> tuple[str, SeverityLevel]:
    for threshold, desc, level in HUMIDITY_LEVELS:
        if h < threshold:
            return (desc, level)
    return ("🌊 Basically Underwater 🌊", SeverityLevel.EXTREME)


# ============================================================================
# WEATHER API & CACHE
# ============================================================================

class WeatherAPIError(Exception):
    pass


def _api_url() -> str:
    base = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude":  Config.WEATHER_LAT,
        "longitude": Config.WEATHER_LON,
        "current":   "temperature_2m,relative_humidity_2m,apparent_temperature,"
                     "precipitation,weather_code,wind_speed_10m,wind_direction_10m,uv_index",
        "hourly":    "temperature_2m,weather_code,precipitation_probability,precipitation,is_day",
        "daily":     "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max,sunrise,sunset",
        "timezone":  "auto",
    }
    return base + "?" + "&".join(f"{k}={v}" for k, v in params.items())


def _fetch() -> dict:
    headers = {"User-Agent": "WaybarClockWeather/1.0", "Accept": "application/json"}
    last: Optional[Exception] = None
    for attempt in range(2):
        try:
            req = Request(_api_url(), headers=headers, method="GET")
            with urlopen(req, timeout=Config.API_TIMEOUT) as r:
                if r.status != 200:
                    raise WeatherAPIError(f"HTTP {r.status}")
                return json.loads(r.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as e:
            last = e
            if attempt == 0:
                time.sleep(0.5)
    raise WeatherAPIError(f"Failed: {last}")


def get_weather_data() -> Optional[dict]:
    cache = Config.CACHE_FILE
    cache.parent.mkdir(parents=True, exist_ok=True)
    try:
        if cache.exists() and (time.time() - cache.stat().st_mtime) < Config.CACHE_TTL:
            return json.loads(cache.read_text())
    except Exception:
        pass
    try:
        data = _fetch()
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(cache)
        return data
    except Exception as e:
        print(f"Weather error: {e}", file=sys.stderr)
        try:
            return json.loads(cache.read_text()) if cache.exists() else None
        except Exception:
            return None


# ============================================================================
# WEATHER PARSING
# ============================================================================

def parse_current(data: dict) -> CurrentWeather:
    c = data["current"]
    return CurrentWeather(
        temp=float(c["temperature_2m"]),
        feels_like=float(c["apparent_temperature"]),
        humidity=int(c["relative_humidity_2m"]),
        wind=WindInfo(float(c["wind_speed_10m"]), float(c["wind_direction_10m"])),
        uv_index=float(c.get("uv_index", 0)),
        condition=WeatherCondition.from_code(int(c["weather_code"])),
        precipitation=float(c.get("precipitation", 0)),
    )


def parse_hourly(data: dict, now: datetime) -> list[dict]:
    h = data["hourly"]
    times = h["time"]
    iso = now.strftime("%Y-%m-%dT%H")
    start = next((i for i, t in enumerate(times) if t.startswith(iso)), 0)
    return [
        {
            "time":       datetime.fromisoformat(times[i]),
            "temp":       float(h["temperature_2m"][i]),
            "code":       int(h["weather_code"][i]),
            "precip_prob":int(h["precipitation_probability"][i]),
        }
        for i in range(start, min(start + 12, len(times)))
    ]


def parse_daily(data: dict) -> list[dict]:
    d = data["daily"]
    rain_probs = d.get("precipitation_probability_max", [0] * len(d["time"]))
    return [
        {
            "date":     datetime.fromisoformat(d["time"][i]),
            "code":     int(d["weather_code"][i]),
            "temp_max": float(d["temperature_2m_max"][i]),
            "temp_min": float(d["temperature_2m_min"][i]),
            "rain_prob":int(rain_probs[i]) if i < len(rain_probs) else 0,
        }
        for i in range(1, min(7, len(d["time"])))
    ]


# ============================================================================
# WEATHER FORMATTING
# ============================================================================

def fmt_hourly_line(h: dict) -> str:
    dt   = h["time"]
    icon = CLOCK_ICONS[dt.hour % 12]
    rc   = THEME.blue if h["precip_prob"] > 0 else THEME.bright_black
    rain = f"<span foreground='{rc}'> {h['precip_prob']:>2}%</span>"
    tc   = temp_color(h["temp"])
    t    = f"<span foreground='{tc}'>{h['temp']:>5.1f}°C</span>"
    cond = WeatherCondition.from_code(h["code"])
    desc = cond.description[:14] + ".." if len(cond.description) > 16 else cond.description
    return (
        f"<span font_family='monospace'>"
        f"{icon} {dt.strftime('%H:%M'):<6}   {rain}   {t}   {cond.icon}  {html.escape(desc)}"
        f"</span>"
    )


def fmt_daily_line(d: dict) -> str:
    dt   = d["date"]
    cond = WeatherCondition.from_code(d["code"])
    badge = f"<span background='{THEME.white}' foreground='{THEME.black}'> {dt.strftime('%d')} </span>"
    rc    = THEME.blue if d["rain_prob"] > 0 else THEME.bright_black
    rain  = f"<span foreground='{rc}'> {d['rain_prob']:>2}%</span>"
    mn    = f"<span foreground='{temp_color(d['temp_min'])}'>{d['temp_min']:>2.0f}</span>"
    mx    = f"<span foreground='{temp_color(d['temp_max'])}'>{d['temp_max']:>2.0f}</span>"
    desc  = cond.description[:12] + ".." if len(cond.description) > 14 else cond.description
    return (
        f"<span font_family='monospace'>"
        f"{badge}  {cal_mod.day_name[dt.weekday()]:<10}   {rain}    {mn}  {mx}   {cond.icon}  {html.escape(desc)}"
        f"</span>"
    )


# ============================================================================
# MOON PHASE
# ============================================================================

class MoonPhase(Enum):
    NEW             = ("New Moon",        "🌑", 0.00, 0.03)
    WAXING_CRESCENT = ("Waxing Crescent", "🌒", 0.03, 0.22)
    FIRST_QUARTER   = ("First Quarter",   "🌓", 0.22, 0.28)
    WAXING_GIBBOUS  = ("Waxing Gibbous",  "🌔", 0.28, 0.47)
    FULL            = ("Full Moon",       "🌕", 0.47, 0.53)
    WANING_GIBBOUS  = ("Waning Gibbous",  "🌖", 0.53, 0.72)
    LAST_QUARTER    = ("Last Quarter",    "🌗", 0.72, 0.78)
    WANING_CRESCENT = ("Waning Crescent", "🌘", 0.78, 0.97)
    NEW_END         = ("New Moon",        "🌑", 0.97, 1.00)

    def __init__(self, label: str, emoji: str, start: float, end: float):
        self.label = label
        self.emoji = emoji
        self.start = start
        self.end   = end

    @classmethod
    def from_phase(cls, p: float) -> MoonPhase:
        p = p % 1.0
        for m in cls:
            if m.start <= p < m.end:
                return m
        return cls.NEW


def calc_moon(now: datetime) -> dict:
    days  = (now - Config.NEW_MOON_REFERENCE).total_seconds() / 86400.0
    age   = days % Config.LUNAR_CYCLE_DAYS
    phase = age / Config.LUNAR_CYCLE_DAYS
    illum = phase * 100 if phase <= 0.5 else (1 - phase) * 100
    mp    = MoonPhase.from_phase(phase)

    def next_phase(offset: float) -> datetime:
        cycles = (days - offset) / Config.LUNAR_CYCLE_DAYS
        ts = (Config.NEW_MOON_REFERENCE.timestamp() +
              ((int(cycles) + 1) * Config.LUNAR_CYCLE_DAYS + offset) * 86400.0)
        return datetime.fromtimestamp(ts)

    return {
        "phase":      mp,
        "illum":      illum,
        "next_full":  next_phase(Config.FULL_MOON_OFFSET),
        "next_new":   next_phase(0.0),
    }


# ============================================================================
# CALENDAR GRID
# ============================================================================

def build_calendar(now: datetime) -> str:
    c    = THEME
    cal  = cal_mod.Calendar(firstweekday=cal_mod.MONDAY)
    weeks = cal.monthdayscalendar(now.year, now.month)
    lines: list[str] = []

    # Header
    next_m = now.month + 1 if now.month < 12 else 1
    next_y = now.year if now.month < 12 else now.year + 1
    lines.append(
        f"<span foreground='{c.cyan}' size='large'>{Config.ICON_CLOCK}</span> "
        f"<span foreground='{c.white}' size='large' weight='bold'>{cal_mod.month_name[now.month]}</span> "
        f"<span foreground='{c.bright_black}' size='large'>{now.year}</span>"
    )
    lines.append("")

    # Weekday row — each slot is 8 chars, content centered
    SLOT = 8
    def center(text: str) -> tuple[str, str]:
        l = (SLOT - len(text)) // 2
        r = SLOT - len(text) - l
        return " " * l, " " * r

    day_headers = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    parts = []
    for i, d in enumerate(day_headers):
        col = c.red if i >= 5 else c.yellow
        w   = "bold" if i >= 5 else "normal"
        lp, rp = center(d)
        parts.append(f"{lp}<span foreground='{col}' weight='{w}' font_family='monospace'>{d}</span>{rp}")
    lines.append(f"<span font_family='monospace'>{''.join(parts)}</span>")
    lines.append(hr())

    # Day grid — numbers centered within each slot
    for week in weeks:
        cells = []
        for idx, day in enumerate(week):
            if day == 0:
                cells.append(" " * SLOT)
                continue
            ds = str(day)
            lp, rp = center(ds)
            if day == now.day:
                cells.append(
                    f"{lp}<span foreground='{c.black}' background='{c.cyan}' "
                    f"weight='bold' font_family='monospace'>{ds}</span>{rp}"
                )
            elif idx >= 5:
                cells.append(f"{lp}<span foreground='{c.red}' font_family='monospace'>{ds}</span>{rp}")
            else:
                cells.append(f"{lp}<span foreground='{c.white}' font_family='monospace'>{ds}</span>{rp}")
        lines.append(f"<span font_family='monospace'>{''.join(cells)}</span>")

    # Next month
    lines.append("")
    lines.append(f"<span foreground='{c.green}'><b>{Config.ICON_CALENDAR} Next Month</b></span>")
    lines.append(
        f"<span foreground='{c.bright_black}'>"
        f"{cal_mod.month_name[next_m][:3]} {next_y}</span>"
    )
    return "\n".join(lines)


# ============================================================================
# SYSTEM INFO
# ============================================================================

def get_uptime() -> Optional[str]:
    try:
        s = float(Path("/proc/uptime").read_text().split()[0])
        h = int(s // 3600)
        d, rh = divmod(h, 24)
        m = int((s % 3600) // 60)
        return f"{d}d {rh}h {m}m" if d else f"{h}h {m}m"
    except Exception:
        return None


def get_load() -> Optional[str]:
    try:
        return " ".join(Path("/proc/loadavg").read_text().split()[:3])
    except Exception:
        return None


# ============================================================================
# TOOLTIP
# ============================================================================

def hr(width: int = Config.SEPARATOR_WIDTH) -> str:
    return f"<span foreground='{THEME.bright_black}'>{'─' * width}</span>"


def build_tooltip(
    current: Optional[CurrentWeather],
    hourly: list[dict],
    daily: list[dict],
    sunrise: str,
    sunset: str,
    now: datetime,
) -> str:
    c = THEME
    lines: list[str] = []

    # ── Weather ──────────────────────────────────────────────────────────────
    if current:
        lines.append(
            f"<span size='large' foreground='{c.yellow}'><b>"
            f" {html.escape(Config.WEATHER_CITY)} │ "
            f"{current.condition.icon} {html.escape(current.condition.description)}"
            f"</b></span>"
        )
        lines.append(hr())
        lines.append(f" {fmt_temp(current.temp)} (Feels {fmt_temp(current.feels_like)})")
        lines.append(html.escape(f" {sunrise}    {sunset}"))
        lines.append("")

        uv_desc, uv_lvl     = get_uv_info(current.uv_index)
        hum_desc, hum_lvl   = get_humidity_info(current.humidity)
        fire_desc, fire_lvl = current.fire_danger

        lines.append(f" {fmt_sev(current.humidity, hum_lvl, '%')} {html.escape(hum_desc)}")
        lines.append(
            f"󰖝 {fmt_sev(f'{current.wind.arrow} {current.wind.direction} {current.wind.speed_kph:.0f}km/h', current.wind.severity)}"
            f" ({current.wind.severity.name.replace('_', ' ').title()})"
        )
        lines.append(f"󰓄 {fmt_sev(f'UV: {current.uv_index:.1f}', uv_lvl)} ({uv_desc})")
        lines.append(f"󱗗 {fmt_sev(f'Fire: {fire_desc}', fire_lvl)}")
        lines.append(hr())

        lines.append(f"<span size='large' foreground='{c.yellow}'><b> Today</b></span>")
        lines.append(hr())
        for h in hourly:
            lines.append(fmt_hourly_line(h))

        lines.append(hr())
        lines.append(f"<span size='large' foreground='{c.blue}'><b> Extended Forecast</b></span>")
        lines.append(hr())
        for i, d in enumerate(daily):
            lines.append(fmt_daily_line(d))
            if i < len(daily) - 1:
                lines.append("")
        lines.append(hr())
    else:
        lines.append(f"<span foreground='{c.bright_black}'>Weather unavailable</span>")
        lines.append(hr())

    # ── Calendar ─────────────────────────────────────────────────────────────
    lines.append(build_calendar(now))
    lines.append(hr())

    # ── Moon ─────────────────────────────────────────────────────────────────
    moon = calc_moon(now)
    mp: MoonPhase = moon["phase"]
    illum: float  = moon["illum"]
    d_full = (moon["next_full"] - now).days
    d_new  = (moon["next_new"]  - now).days
    bar = "█" * int(illum / 10) + "░" * (10 - int(illum / 10)) + f" {illum:.0f}%"
    lines += [
        f"<span foreground='{c.yellow}' size='large' weight='bold'>{Config.ICON_MOON} Moon Phase</span>",
        hr(),
        f"<span foreground='{c.white}' size='large'>{mp.emoji} <b>{mp.label}</b></span>",
        "",
        f"<span foreground='{c.cyan}' font_family='monospace'>  {bar}</span>",
        "",
        f"<span foreground='{c.bright_black}'>  🌕 Full Moon in <span foreground='{c.white}'>{d_full} days</span></span>",
        f"<span foreground='{c.bright_black}'>  🌑 New Moon in  <span foreground='{c.white}'>{d_new} days</span></span>",
    ]

    # ── System ───────────────────────────────────────────────────────────────
    uptime = get_uptime()
    load   = get_load()
    if uptime or load:
        lines.append(hr())
        lines.append(f"<span foreground='{c.green}' weight='bold'>{Config.ICON_UPTIME} System Status</span>")
        lines.append(hr())
        if uptime:
            lines.append(f"<span foreground='{c.cyan}'>  {Config.ICON_UPTIME} <b>Uptime:</b> {uptime}</span>")
        if load:
            lines.append(f"<span foreground='{c.bright_black}'>  Load: {load}</span>")

    return "\n".join(lines)


# ============================================================================
# BAR TEXT
# ============================================================================

def build_text(now: datetime, current: Optional[CurrentWeather]) -> str:
    c    = THEME
    time = now.strftime("%H:%M")
    date = now.strftime("%a, %b %d")

    clock = (
        f"{Config.ICON_CLOCK} "
        f"<span foreground='{c.cyan}' weight='bold'>{time}</span> "
        f"<span foreground='{c.bright_black}'>│</span> "
        f"<span foreground='{c.white}'>{date}</span>"
    )

    if current is None:
        return clock

    tc = temp_color(current.temp)
    weather = (
        f"<span foreground='{c.bright_black}'>│</span> "
        f"{current.condition.icon} "
        f"<span foreground='{tc}'>{current.temp:.0f}°C</span>"
    )
    return f"{clock}  {weather}"


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:
    now     = datetime.now()
    now_utc = datetime.now(timezone.utc).astimezone()

    weather_data = get_weather_data()
    current: Optional[CurrentWeather] = None
    hourly:  list[dict] = []
    daily:   list[dict] = []
    sunrise = sunset = "N/A"

    if weather_data:
        try:
            current = parse_current(weather_data)
            hourly  = parse_hourly(weather_data, now_utc)
            daily   = parse_daily(weather_data)
            sunrise = weather_data["daily"]["sunrise"][0].split("T")[1][:5]
            sunset  = weather_data["daily"]["sunset"][0].split("T")[1][:5]
        except Exception as e:
            print(f"Parse error: {e}", file=sys.stderr)

    print(json.dumps({
        "text":    build_text(now, current),
        "tooltip": f"<span size='12000'>{build_tooltip(current, hourly, daily, sunrise, sunset, now)}</span>",
        "markup":  "pango",
        "class":   "clock-weather",
        "alt":     now.strftime("%Y-%m-%d"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
