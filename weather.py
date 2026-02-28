#!/usr/bin/env python3
"""
Waybar Weather Module - Optimized Version

A high-performance, maintainable weather display module for Waybar.
Features Open-Meteo API integration with intelligent caching, Pango markup,
and comprehensive error handling.
"""

from __future__ import annotations

import calendar
import json
import sys
import os
import re
import time
import html
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Final, Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Optional: tomllib for reading theme (Python 3.11+)
try:
    import tomllib
except ImportError:
    tomllib = None


# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass(frozen=True)
class Config:
    """Immutable configuration container.

    Personal values are read from environment variables so the script
    stays generic. Set these in ~/.config/hypr/env.conf (Hyprland) or
    your shell profile, then restart Waybar:

        WAYBAR_WEATHER_LAT   Your latitude  (e.g. 48.8566)
        WAYBAR_WEATHER_LON   Your longitude (e.g. 2.3522)
        WAYBAR_WEATHER_CITY  Label shown in the bar (e.g. Paris)
    """
    lat: float          = float(os.environ.get("WAYBAR_WEATHER_LAT",  "0.0"))
    lon: float          = float(os.environ.get("WAYBAR_WEATHER_LON",  "0.0"))
    display_name: str   = os.environ.get("WAYBAR_WEATHER_CITY", "My City")
    cache_timeout: int = 900  # 15 minutes
    cache_file: Path = Path.home() / ".cache" / "waybar_weather" / "data.json"
    theme_file: Path = Path.home() / ".config/omarchy/current/theme/alacritty.toml"
    api_timeout: int = 10
    
    def __post_init__(self) -> None:
        # Ensure cache directory exists
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)


CONFIG: Final = Config()


# ============================================================================
# DATA MODELS
# ============================================================================

class SeverityLevel(Enum):
    """Standardized severity levels with associated colors."""
    LOW = ("green", 1)
    MODERATE = ("yellow", 2)
    HIGH = ("orange", 3)
    VERY_HIGH = ("red", 4)
    EXTREME = ("purple", 5)
    CATASTROPHIC = ("purple", 6)
    
    def __init__(self, color_key: str, rank: int) -> None:
        self.color_key = color_key
        self.rank = rank


@dataclass(frozen=True)
class WeatherCondition:
    """Immutable weather condition representation."""
    code: int
    icon: str
    description: str
    
    @classmethod
    def from_code(cls, code: int) -> WeatherCondition:
        """Factory method with safe fallback."""
        return WEATHER_MAP.get(code, cls(code, "❓", "Unknown"))


@dataclass(frozen=True)
class WindInfo:
    """Wind data with derived properties."""
    speed_kph: float
    direction_deg: float
    
    @property
    def direction(self) -> str:
        """Calculate compass direction with bounds checking."""
        # Fix: Proper bounds handling for 360-degree wrap
        normalized = self.direction_deg % 360
        idx = int((normalized + 11.25) / 22.5) % 16
        return WIND_DIRECTIONS[idx]
    
    @property
    def arrow(self) -> str:
        """Get directional arrow for current direction."""
        return WIND_ARROWS.get(self.direction, "○")
    
    @property
    def severity(self) -> SeverityLevel:
        """Calculate wind severity level."""
        speed = self.speed_kph
        thresholds = [
            (10, SeverityLevel.LOW),
            (20, SeverityLevel.LOW),
            (30, SeverityLevel.MODERATE),
            (40, SeverityLevel.MODERATE),
            (50, SeverityLevel.HIGH),
            (63, SeverityLevel.HIGH),
            (75, SeverityLevel.VERY_HIGH),
            (89, SeverityLevel.VERY_HIGH),
            (103, SeverityLevel.EXTREME),
        ]
        for threshold, level in thresholds:
            if speed < threshold:
                return level
        return SeverityLevel.CATASTROPHIC


@dataclass(frozen=True)
class CurrentWeather:
    """Current weather snapshot."""
    temp: float
    feels_like: float
    humidity: int
    wind: WindInfo
    uv_index: float
    condition: WeatherCondition
    precipitation: float
    
    @property
    def fire_danger(self) -> tuple[str, SeverityLevel]:
        """
        Calculate fire danger index.
        Formula: weighted combination of temp, wind, and inverse humidity.
        """
        if self.humidity > 70:
            return ("Low-Moderate", SeverityLevel.LOW)
        
        # Documented formula with safety bounds
        danger_score = (self.temp * 0.5) + (self.wind.speed_kph * 0.8) - (self.humidity * 0.5)
        
        thresholds = [
            (12, SeverityLevel.LOW),
            (24, SeverityLevel.HIGH),
            (38, SeverityLevel.VERY_HIGH),
            (50, SeverityLevel.EXTREME),
            (75, SeverityLevel.EXTREME),
        ]
        for threshold, level in thresholds:
            if danger_score < threshold:
                desc = level.name.replace("_", " ").title()
                return (desc, level)
        
        return ("Catastrophic", SeverityLevel.CATASTROPHIC)


# ============================================================================
# CONSTANTS & MAPPINGS
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
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"
]

WIND_ARROWS: Final[dict[str, str]] = {
    "N": "↑", "NNE": "↗", "NE": "↗", "ENE": "↗",
    "E": "→", "ESE": "↘", "SE": "↘", "SSE": "↘",
    "S": "↓", "SSW": "↙", "SW": "↙", "WSW": "↙",
    "W": "←", "WNW": "↖", "NW": "↖", "NNW": "↖"
}

CLOCK_ICONS: Final[list[str]] = [
    "󱑊", "󱐿", "󱑀", "󱑁", "󱑂", "󱑃", 
    "󱑄", "󱑅", "󱑆", "󱑇", "󱑈", "󱑉"
]

UV_THRESHOLDS: Final[list[tuple[float, str, SeverityLevel]]] = [
    (3, "Low", SeverityLevel.LOW),
    (6, "Moderate", SeverityLevel.MODERATE),
    (8, "High", SeverityLevel.HIGH),
    (11, "Very High", SeverityLevel.VERY_HIGH),
]

HUMIDITY_LEVELS: Final[list[tuple[int, str, SeverityLevel]]] = [
    (20, " Extreme Dry ", SeverityLevel.EXTREME),
    (30, "⚡ Very Dry ⚡", SeverityLevel.VERY_HIGH),
    (40, " Pleasant ", SeverityLevel.HIGH),
    (50, " Perfect ", SeverityLevel.LOW),
    (60, " Little Bit Humid ", SeverityLevel.MODERATE),
    (70, " Getting Sticky ", SeverityLevel.MODERATE),
    (80, " Properly Humid Now ", SeverityLevel.MODERATE),
    (90, " Tropical Sauna Mode ", SeverityLevel.HIGH),
]


# ============================================================================
# THEME & COLOR MANAGEMENT
# ============================================================================

@dataclass(frozen=True)
class ColorTheme:
    """Immutable color theme container."""
    white: str = "#ffffff"
    red: str = "#ff0000"
    yellow: str = "#ffff00"
    green: str = "#00ff00"
    blue: str = "#0000ff"
    cyan: str = "#00ffff"
    purple: str = "#ca9ee6"
    bright_black: str = "#555555"
    orange: str = "#ef9f76"
    
    def get(self, key: str) -> str:
        """Safe color retrieval with fallback to white."""
        return getattr(self, key, self.white)
    
    @classmethod
    def from_omarchy(cls, theme_path: Path) -> ColorTheme:
        """Load theme from TOML file with comprehensive error handling."""
        defaults = cls()
        
        if not tomllib or not theme_path.exists():
            return defaults
        
        try:
            content = theme_path.read_text(encoding="utf-8")
            data = tomllib.loads(content)
            colors = data.get("colors", {})
            normal = colors.get("normal", {})
            bright = colors.get("bright", {})
            
            return cls(
                white=normal.get("white", defaults.white),
                red=normal.get("red", defaults.red),
                yellow=normal.get("yellow", defaults.yellow),
                green=normal.get("green", defaults.green),
                blue=normal.get("blue", defaults.blue),
                cyan=normal.get("cyan", defaults.cyan),
                purple=normal.get("magenta", defaults.purple),
                bright_black=bright.get("black", defaults.bright_black),
                orange=defaults.orange,  # Custom mapping not in standard theme
            )
        except (tomllib.TOMLDecodeError, UnicodeDecodeError, KeyError) as e:
            # Log to stderr for debugging but don't crash
            print(f"Theme load warning: {e}", file=sys.stderr)
            return defaults


THEME: Final = ColorTheme.from_omarchy(CONFIG.theme_file)


# ============================================================================
# TEMPERATURE COLOR MAPPING
# ============================================================================

@dataclass(frozen=True)
class TempColorMap:
    """Temperature-to-color mapping with interpolation support."""
    thresholds: list[tuple[float, str]]
    
    def get_color(self, temp: float) -> str:
        """Get color for temperature using nearest threshold."""
        for max_temp, color in self.thresholds:
            if temp <= max_temp:
                return color
        return self.thresholds[-1][1] if self.thresholds else THEME.red


TEMP_COLORS: Final = TempColorMap([
    (15, THEME.blue),
    (18, THEME.blue),
    (21, THEME.cyan),
    (24, THEME.cyan),
    (27, THEME.green),
    (30, THEME.yellow),
    (32, THEME.yellow),
    (33, THEME.red),
    (100, THEME.red),
])


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def get_uv_info(uv_index: float) -> tuple[str, SeverityLevel]:
    """Get UV description and severity level."""
    for threshold, desc, level in UV_THRESHOLDS:
        if uv_index < threshold:
            return (desc, level)
    return ("Extreme", SeverityLevel.EXTREME)


def get_humidity_info(humidity: int) -> tuple[str, SeverityLevel]:
    """Get humidity description and severity level."""
    for threshold, desc, level in HUMIDITY_LEVELS:
        if humidity < threshold:
            return (desc, level)
    return ("🌊 Basically Underwater 🌊", SeverityLevel.EXTREME)


def format_temp(temp: float) -> str:
    """Format temperature with appropriate color."""
    color = TEMP_COLORS.get_color(temp)
    return f"<span foreground='{color}'>{temp:.1f}°C</span>"


def format_severity(value: Any, level: SeverityLevel, suffix: str = "") -> str:
    """Format a value with its severity color."""
    color = THEME.get(level.color_key)
    return f"<span foreground='{color}'>{value}{suffix}</span>"


# ============================================================================
# API & CACHING
# ============================================================================

class WeatherAPIError(Exception):
    """Custom exception for weather API failures."""
    pass


class CacheManager:
    """JSON-based cache manager with atomic writes and corruption recovery."""
    
    def __init__(self, cache_path: Path, timeout: int):
        self.path = cache_path
        self.timeout = timeout
        self.meta_path = cache_path.with_suffix(".meta")
    
    def _is_valid(self) -> bool:
        """Check if cache exists and is not expired."""
        if not self.path.exists():
            return False
        
        try:
            mtime = self.path.stat().st_mtime
            return (time.time() - mtime) < self.timeout
        except OSError:
            return False
    
    def load(self) -> Optional[dict]:
        """Load cached data with corruption handling."""
        if not self._is_valid():
            return None
        
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            # Corrupted cache - remove and return None
            print(f"Cache corrupted, refreshing: {e}", file=sys.stderr)
            self.clear()
            return None
    
    def save(self, data: dict) -> None:
        """Atomic cache write to prevent corruption during write."""
        tmp_path = self.path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            # Atomic rename on POSIX systems
            tmp_path.replace(self.path)
        except OSError as e:
            print(f"Cache write failed: {e}", file=sys.stderr)
            # Clean up temp file if exists
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
    
    def clear(self) -> None:
        """Clear cache files."""
        for p in [self.path, self.meta_path]:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def build_api_url(lat: float, lon: float) -> str:
    """Construct Open-Meteo API URL with required parameters."""
    base = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": ",".join([
            "temperature_2m", "relative_humidity_2m", "apparent_temperature",
            "precipitation", "rain", "weather_code", "wind_speed_10m",
            "wind_direction_10m", "uv_index"
        ]),
        "hourly": ",".join([
            "temperature_2m", "weather_code", "precipitation_probability",
            "precipitation", "is_day"
        ]),
        "daily": ",".join([
            "weather_code", "temperature_2m_max", "temperature_2m_min",
            "precipitation_probability_max", "sunrise", "sunset"
        ]),
        "timezone": "auto",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{base}?{query}"


def fetch_weather_data(url: str, timeout: int) -> dict:
    """
    Fetch weather data with retry logic and proper error handling.
    Uses standard library only for reduced dependency footprint.
    """
    headers = {
        "User-Agent": "WaybarWeatherModule/2.0",
        "Accept": "application/json",
    }
    
    last_error: Optional[Exception] = None
    
    # Simple retry: 2 attempts
    for attempt in range(2):
        try:
            req = Request(url, headers=headers, method="GET")
            with urlopen(req, timeout=timeout) as response:
                if response.status != 200:
                    raise WeatherAPIError(f"HTTP {response.status}")
                data = json.loads(response.read().decode("utf-8"))
                return data
        except (HTTPError, URLError, TimeoutError) as e:
            last_error = e
            if attempt == 0:
                time.sleep(0.5)  # Brief delay before retry
            continue
        except json.JSONDecodeError as e:
            raise WeatherAPIError(f"Invalid JSON response: {e}")
    
    raise WeatherAPIError(f"Failed after retries: {last_error}")


def get_weather_data() -> Optional[dict]:
    """Get weather data with caching and fallback."""
    cache = CacheManager(CONFIG.cache_file, CONFIG.cache_timeout)
    
    # Try cache first
    cached = cache.load()
    if cached is not None:
        return cached
    
    # Fetch fresh data
    url = build_api_url(CONFIG.lat, CONFIG.lon)
    try:
        data = fetch_weather_data(url, CONFIG.api_timeout)
        cache.save(data)
        return data
    except WeatherAPIError as e:
        print(f"Weather API error: {e}", file=sys.stderr)
        return None


# ============================================================================
# DATA PARSING
# ============================================================================

def parse_current_weather(data: dict) -> CurrentWeather:
    """Parse current weather from API response with validation."""
    try:
        curr = data["current"]
        
        # Required fields with type coercion
        temp = float(curr["temperature_2m"])
        feels_like = float(curr["apparent_temperature"])
        humidity = int(curr["relative_humidity_2m"])
        wind_speed = float(curr["wind_speed_10m"])
        wind_dir = float(curr["wind_direction_10m"])
        uv = float(curr.get("uv_index", 0))
        code = int(curr["weather_code"])
        
        return CurrentWeather(
            temp=temp,
            feels_like=feels_like,
            humidity=humidity,
            wind=WindInfo(wind_speed, wind_dir),
            uv_index=uv,
            condition=WeatherCondition.from_code(code),
            precipitation=float(curr.get("precipitation", 0)),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise WeatherAPIError(f"Invalid current weather data: {e}")


def parse_hourly_data(data: dict, current_time: datetime) -> list[dict]:
    """
    Parse hourly forecast starting from current hour.
    Returns structured data for next 24 hours.
    """
    try:
        hourly = data["hourly"]
        times = hourly["time"]
        temps = hourly["temperature_2m"]
        codes = hourly["weather_code"]
        probs = hourly["precipitation_probability"]
        
        # Find index of current hour using proper ISO parsing
        current_iso = current_time.strftime("%Y-%m-%dT%H")
        start_idx = None
        
        for i, t in enumerate(times):
            if t.startswith(current_iso):
                start_idx = i
                break
        
        if start_idx is None:
            # Fallback: find nearest future hour
            now_ts = current_time.timestamp()
            for i, t in enumerate(times):
                dt = datetime.fromisoformat(t)
                if dt.timestamp() >= now_ts:
                    start_idx = i
                    break
            if start_idx is None:
                start_idx = 0
        
        # Extract next 24 hours
        result = []
        for i in range(start_idx, min(start_idx + 24, len(times))):
            result.append({
                "time": datetime.fromisoformat(times[i]),
                "temp": float(temps[i]),
                "code": int(codes[i]),
                "precip_prob": int(probs[i]),
            })
        
        return result
    except (KeyError, IndexError, ValueError) as e:
        raise WeatherAPIError(f"Invalid hourly data: {e}")


def parse_daily_data(data: dict) -> list[dict]:
    """Parse daily forecast for next 7 days."""
    try:
        daily = data["daily"]
        times = daily["time"]
        codes = daily["weather_code"]
        max_temps = daily["temperature_2m_max"]
        min_temps = daily["temperature_2m_min"]
        rain_probs = daily.get("precipitation_probability_max", [0] * len(times))
        
        result = []
        for i in range(1, min(7, len(times))):  # Skip today (index 0)
            result.append({
                "date": datetime.fromisoformat(times[i]),
                "code": int(codes[i]),
                "temp_max": float(max_temps[i]),
                "temp_min": float(min_temps[i]),
                "rain_prob": int(rain_probs[i]) if i < len(rain_probs) else 0,
            })
        
        return result
    except (KeyError, IndexError, ValueError) as e:
        raise WeatherAPIError(f"Invalid daily data: {e}")


# ============================================================================
# OUTPUT FORMATTING
# ============================================================================

SEPARATOR_WIDTH: Final = 50


class TooltipBuilder:
    """Builder pattern for constructing Pango-formatted tooltips."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def add_header(self, text: str, color: str = THEME.yellow) -> None:
        """Add a large, colored header."""
        self.lines.append(f"<span size='large' foreground='{color}'><b>{html.escape(text)}</b></span>")

    def add_line(self, text: str) -> None:
        """Add a regular line with HTML escaping."""
        self.lines.append(html.escape(text))

    def add_raw(self, text: str) -> None:
        """Add raw HTML (use with caution)."""
        self.lines.append(text)

    def add_separator(self) -> None:
        """Add visual spacing."""
        self.lines.append("")

    def add_divider(self) -> None:
        """Add a horizontal rule separator line."""
        self.lines.append(f"<span foreground='{THEME.bright_black}'>{'─' * SEPARATOR_WIDTH}</span>")

    def build(self) -> str:
        """Finalize tooltip content."""
        return "\n".join(self.lines)


def format_hourly_line(hour_data: dict, is_tomorrow: bool = False) -> str:
    """Format a single hour entry with monospace alignment."""
    dt = hour_data["time"]
    temp = hour_data["temp"]
    code = hour_data["code"]
    prob = hour_data["precip_prob"]
    
    condition = WeatherCondition.from_code(code)
    clock_idx = dt.hour % 12
    clock_icon = CLOCK_ICONS[clock_idx]
    
    # Time formatting
    time_str = dt.strftime(f"{clock_icon} %H:%M")
    
    # Rain probability with color
    rain_color = THEME.blue if prob > 0 else THEME.bright_black
    rain_str = f"<span foreground='{rain_color}'> {prob:>2}%</span>"
    
    # Temperature with color - format manually for proper alignment
    temp_color = TEMP_COLORS.get_color(temp)
    temp_str = f"<span foreground='{temp_color}'>{temp:>5.1f}°C</span>"

    # Truncate long descriptions
    desc = condition.description
    if len(desc) > 16:
        desc = desc[:14] + ".."

    # Monospace alignment for consistent columns with better spacing
    return (
        f"<span font_family='monospace'>"
        f"{time_str:<14}   {rain_str}   {temp_str}   {condition.icon}  {html.escape(desc)}"
        f"</span>"
    )


def format_daily_line(day_data: dict) -> str:
    """Format a single day entry for extended forecast."""
    dt = day_data["date"]
    code = day_data["code"]
    t_min = day_data["temp_min"]
    t_max = day_data["temp_max"]
    prob = day_data["rain_prob"]
    
    condition = WeatherCondition.from_code(code)
    
    # Calendar-style day number
    day_num = dt.strftime("%d")
    day_name = calendar.day_name[dt.weekday()]
    
    # Styled day badge
    day_badge = f"<span background='{THEME.white}' foreground='#1e1e2e'> {day_num} </span>"
    
    # Rain probability
    rain_color = THEME.blue if prob > 0 else THEME.bright_black
    rain_str = f"<span foreground='{rain_color}'> {prob:>2}%</span>"
    
    # Temperature range
    min_color = TEMP_COLORS.get_color(t_min)
    max_color = TEMP_COLORS.get_color(t_max)
    temp_str = f" <span foreground='{min_color}'>{t_min:>2.0f}</span>  <span foreground='{max_color}'>{t_max:>2.0f}</span>"
    
    # Truncate long descriptions to prevent line wrapping
    desc = condition.description
    if len(desc) > 14:
        desc = desc[:12] + ".."

    return (
        f"<span font_family='monospace'>"
        f"{day_badge}  {day_name:<10}   {rain_str}   {temp_str}   {condition.icon}  {html.escape(desc)}"
        f"</span>"
    )


def build_tooltip(
    current: CurrentWeather,
    hourly: list[dict],
    daily: list[dict],
    sunrise: str,
    sunset: str
) -> str:
    """Construct full tooltip content."""
    builder = TooltipBuilder()
    
    # Header section
    location_header = f" {CONFIG.display_name} - {current.condition.icon} {current.condition.description}"
    builder.add_header(location_header)
    builder.add_divider()

    # Current conditions
    temp_line = f" {format_temp(current.temp)} (Feels {format_temp(current.feels_like)})"
    builder.add_raw(temp_line)
    builder.add_line(f"  {sunrise}    {sunset}")
    builder.add_separator()
    
    # Detailed metrics
    uv_desc, uv_level = get_uv_info(current.uv_index)
    hum_desc, hum_level = get_humidity_info(current.humidity)
    fire_desc, fire_level = current.fire_danger
    
    builder.add_raw(
        f" {format_severity(current.humidity, hum_level, '%')} {html.escape(hum_desc)}"
    )
    builder.add_raw(
        f"󰖝 {format_severity(f'{current.wind.arrow} {current.wind.direction} {current.wind.speed_kph:.0f}km/h', current.wind.severity)} "
        f"({current.wind.severity.name.replace('_', ' ').title()})"
    )
    builder.add_raw(
        f"󰓄 {format_severity(f'UV: {current.uv_index:.1f}', uv_level)} ({uv_desc})"
    )
    builder.add_raw(
        f"󱗗 {format_severity(f'Fire: {fire_desc}', fire_level)}"
    )
    builder.add_divider()

    # Today's hourly forecast
    builder.add_header(" Today", THEME.yellow)
    builder.add_divider()
    for hour in hourly[:12]:  # Show next 12 hours to save space
        builder.add_raw(format_hourly_line(hour))
    
    # Extended forecast
    builder.add_divider()
    builder.add_header(" Extended Forecast", THEME.blue)
    builder.add_divider()
    for i, day in enumerate(daily):
        builder.add_raw(format_daily_line(day))
        if i < len(daily) - 1:
            builder.add_separator()
    
    return builder.build()


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def create_error_output(message: str, tooltip: Optional[str] = None) -> None:
    """Output standardized error JSON for Waybar."""
    output = {
        "text": html.escape(message),
        "tooltip": html.escape(tooltip or message),
        "class": "weather-error",
        "markup": "pango",
    }
    print(json.dumps(output, ensure_ascii=False))
    sys.exit(0)


def create_weather_output(current: CurrentWeather, tooltip: str) -> None:
    """Output standardized weather JSON for Waybar."""
    # Main bar display: icon + temperature
    text = f"{current.condition.icon}  <span foreground='{THEME.white}'>{current.temp:.0f}°C</span>  "
    
    output = {
        "text": text,
        "tooltip": f"<span size='12000'>{tooltip}</span>",
        "class": "weather",
        "markup": "pango",
    }
    print(json.dumps(output, ensure_ascii=False))


def main() -> None:
    """Main entry point with comprehensive error handling."""
    try:
        # Fetch data
        data = get_weather_data()
        if data is None:
            create_error_output("N/A", "Weather data unavailable\nCheck connection or cache")
            return
        
        # Parse current conditions
        current = parse_current_weather(data)
        
        # Parse forecasts
        now = datetime.now(timezone.utc).astimezone()
        hourly = parse_hourly_data(data, now)
        daily = parse_daily_data(data)
        
        # Extract sunrise/sunset for today
        try:
            today_data = data["daily"]
            sunrise = today_data["sunrise"][0].split("T")[1][:5]  # HH:MM format
            sunset = today_data["sunset"][0].split("T")[1][:5]
        except (KeyError, IndexError):
            sunrise, sunset = "N/A", "N/A"
        
        # Build and output
        tooltip = build_tooltip(current, hourly, daily, sunrise, sunset)
        create_weather_output(current, tooltip)
        
    except WeatherAPIError as e:
        create_error_output("API Error", str(e))
    except json.JSONDecodeError as e:
        create_error_output("Data Error", f"Failed to parse weather data: {e}")
    except OSError as e:
        create_error_output("System Error", f"File/IO error: {e}")
    except Exception as e:
        # Catch-all for unexpected errors
        create_error_output("Error", f"Unexpected error: {type(e).__name__}: {e}")
        raise  # Re-raise for full traceback in logs


if __name__ == "__main__":
    main()
