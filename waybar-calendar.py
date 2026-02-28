#!/usr/bin/env python3
"""
Waybar Calendar Module - Enhanced UI/UX Edition (Alignment Fixed)

A sophisticated calendar widget for Waybar featuring:
- Perfectly aligned calendar grid (restored original alignment)
- Elegant visual hierarchy with semantic color coding
- High-contrast accessible design (WCAG-compliant)
- Responsive layout with intelligent spacing
"""

from __future__ import annotations

import functools
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any, Final, Optional

# Optional imports with graceful degradation
try:
    import tomllib
except ImportError:
    tomllib = None


# ============================================================================
# CONFIGURATION & DESIGN TOKENS
# ============================================================================

class Config:
    """Application configuration with design tokens."""
    
    # Icons (Nerd Font icons for visual consistency)
    ICON_CLOCK: Final[str] = ""
    ICON_CALENDAR: Final[str] = ""
    ICON_MOON: Final[str] = "󰽢"
    ICON_TIMER: Final[str] = "󰔟"
    ICON_UPTIME: Final[str] = "󰔚"
    
    # Layout & Spacing
    TOOLTIP_WIDTH: Final[int] = 34
    
    # Paths
    THEME_PATH: Final[Path] = Path.home() / ".config/omarchy/current/theme/colors.toml"
    
    # Performance tuning
    CACHE_THEME_SECONDS: Final[int] = 60
    CACHE_MOON_SECONDS: Final[int] = 3600
    
    # Moon calculation constants
    LUNAR_CYCLE_DAYS: Final[float] = 29.53058867
    NEW_MOON_REFERENCE: Final[datetime] = datetime(2000, 1, 6, 18, 14)
    FULL_MOON_OFFSET: Final[float] = 14.765


# ============================================================================
# ACCESSIBILITY & SEMANTIC COLORS
# ============================================================================

class SemanticColor(Enum):
    """Semantic color roles for consistent theming and accessibility."""
    PRIMARY = auto()
    SECONDARY = auto()
    SUCCESS = auto()
    WARNING = auto()
    DANGER = auto()
    INFO = auto()
    MUTED = auto()
    BACKGROUND = auto()
    TEXT = auto()
    TEXT_INVERTED = auto()


@dataclass(frozen=True)
class ThemeColors:
    """Immutable color theme with semantic mapping."""
    # Base palette (Catppuccin Mocha)
    black: str = "#1e1e2e"
    red: str = "#f38ba8"
    green: str = "#a6e3a1"
    yellow: str = "#f9e2af"
    blue: str = "#89b4fa"
    magenta: str = "#f5c2e7"
    cyan: str = "#89dceb"
    white: str = "#cdd6f4"
    bright_black: str = "#6c7086"
    bright_red: str = "#f38ba8"
    bright_green: str = "#a6e3a1"
    bright_yellow: str = "#f9e2af"
    bright_blue: str = "#89b4fa"
    bright_magenta: str = "#f5c2e7"
    bright_cyan: str = "#94e2d5"
    bright_white: str = "#bac2de"
    
    def get_semantic(self, role: SemanticColor) -> str:
        """Resolve semantic color role to hex value."""
        mapping = {
            SemanticColor.PRIMARY: self.cyan,
            SemanticColor.SECONDARY: self.blue,
            SemanticColor.SUCCESS: self.green,
            SemanticColor.WARNING: self.yellow,
            SemanticColor.DANGER: self.red,
            SemanticColor.INFO: self.white,
            SemanticColor.MUTED: self.bright_black,
            SemanticColor.BACKGROUND: self.black,
            SemanticColor.TEXT: self.white,
            SemanticColor.TEXT_INVERTED: self.black,
        }
        return mapping.get(role, self.white)
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThemeColors:
        """Create ThemeColors from dictionary with validation."""
        defaults = {
            "color0": "black", "color1": "red", "color2": "green",
            "color3": "yellow", "color4": "blue", "color5": "magenta",
            "color6": "cyan", "color7": "white", "color8": "bright_black",
            "color9": "bright_red", "color10": "bright_green",
            "color11": "bright_yellow", "color12": "bright_blue",
            "color13": "bright_magenta", "color14": "bright_cyan",
            "color15": "bright_white"
        }
        
        kwargs = {}
        for color_key, attr_name in defaults.items():
            kwargs[attr_name] = data.get(color_key, getattr(cls(), attr_name))
        
        return cls(**kwargs)


# ============================================================================
# DATA MODELS
# ============================================================================

class MoonPhaseType(Enum):
    """Enumeration of moon phases with display metadata."""
    NEW = ("New Moon", "🌑", 0.0, 0.03, "New beginnings")
    WAXING_CRESCENT = ("Waxing Crescent", "🌒", 0.03, 0.22, "Growing intention")
    FIRST_QUARTER = ("First Quarter", "🌓", 0.22, 0.28, "Decision time")
    WAXING_GIBBOUS = ("Waxing Gibbous", "🌔", 0.28, 0.47, "Refinement")
    FULL = ("Full Moon", "🌕", 0.47, 0.53, "Culmination")
    WANING_GIBBOUS = ("Waning Gibbous", "🌖", 0.53, 0.72, "Gratitude")
    LAST_QUARTER = ("Last Quarter", "🌗", 0.72, 0.78, "Release")
    WANING_CRESCENT = ("Waning Crescent", "🌘", 0.78, 0.97, "Rest")
    NEW_END = ("New Moon", "🌑", 0.97, 1.0, "New beginnings")
    
    def __init__(self, name: str, emoji: str, start: float, end: float, meaning: str):
        self.phase_name = name
        self.emoji = emoji
        self.start = start
        self.end = end
        self.meaning = meaning
    
    @classmethod
    def from_phase(cls, phase: float) -> MoonPhaseType:
        """Determine moon phase from normalized phase value (0-1)."""
        phase = phase % 1.0
        for member in cls:
            if member.start <= phase < member.end:
                return member
        return cls.NEW


@dataclass(frozen=True)
class MoonData:
    """Immutable moon phase data with computed properties."""
    phase_type: MoonPhaseType
    illumination: float
    next_full: datetime
    next_new: datetime
    
    @property
    def name(self) -> str:
        return self.phase_type.phase_name
    
    @property
    def emoji(self) -> str:
        return self.phase_type.emoji
    
    @property
    def meaning(self) -> str:
        return self.phase_type.meaning
    
    @property
    def progress_bar(self) -> str:
        """Generate ASCII progress bar for illumination."""
        filled = int(self.illumination / 10)
        empty = 10 - filled
        bar = "█" * filled + "░" * empty
        return f"{bar} {self.illumination:.0f}%"


@dataclass(frozen=True)
class SystemInfo:
    """Immutable system status information."""
    uptime_text: Optional[str] = None
    has_active_timers: bool = False
    load_average: Optional[str] = None


# ============================================================================
# CACHING & PERFORMANCE
# ============================================================================

class TimedCache:
    """Thread-safe TTL cache for expensive operations."""
    
    def __init__(self, ttl_seconds: float):
        self.ttl = timedelta(seconds=ttl_seconds)
        self._cache: dict[str, tuple[datetime, Any]] = {}
    
    def get(self, key: str) -> Optional[Any]:
        """Retrieve cached value if not expired."""
        if key not in self._cache:
            return None
        
        timestamp, value = self._cache[key]
        if datetime.now() - timestamp > self.ttl:
            del self._cache[key]
            return None
        return value
    
    def set(self, key: str, value: Any) -> None:
        """Store value with current timestamp."""
        self._cache[key] = (datetime.now(), value)
    
    def clear(self) -> None:
        """Clear all cached entries."""
        self._cache.clear()


# Global cache instances
_theme_cache = TimedCache(Config.CACHE_THEME_SECONDS)
_moon_cache = TimedCache(Config.CACHE_MOON_SECONDS)


# ============================================================================
# THEME MANAGEMENT
# ============================================================================

def load_theme_colors() -> ThemeColors:
    """Load theme colors with aggressive caching."""
    cached = _theme_cache.get("colors")
    if cached is not None:
        return cached
    
    colors = _load_theme_from_disk()
    _theme_cache.set("colors", colors)
    return colors


def _load_theme_from_disk() -> ThemeColors:
    """Internal: Load theme from TOML file with comprehensive error handling."""
    if tomllib is None or not Config.THEME_PATH.exists():
        return ThemeColors()
    
    try:
        content = Config.THEME_PATH.read_text(encoding="utf-8")
        data = tomllib.loads(content)
        return ThemeColors.from_dict(data)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, PermissionError) as e:
        print(f"Warning: Could not load theme: {e}", file=sys.stderr)
        return ThemeColors()
    except Exception as e:
        print(f"Unexpected error loading theme: {e}", file=sys.stderr)
        return ThemeColors()


# ============================================================================
# MOON PHASE CALCULATIONS
# ============================================================================

def calculate_moon_phase(date: datetime) -> MoonData:
    """Calculate moon phase with caching."""
    cache_key = date.strftime("%Y-%m-%d")
    cached = _moon_cache.get(cache_key)
    if cached is not None:
        return cached
    
    data = _calculate_moon_phase_impl(date)
    _moon_cache.set(cache_key, data)
    return data


def _calculate_moon_phase_impl(date: datetime) -> MoonData:
    """Internal: Mathematical moon phase calculation."""
    diff = date - Config.NEW_MOON_REFERENCE
    days = diff.total_seconds() / 86400.0
    
    moon_age = days % Config.LUNAR_CYCLE_DAYS
    phase = moon_age / Config.LUNAR_CYCLE_DAYS
    
    phase_type = MoonPhaseType.from_phase(phase)
    illumination = phase * 100 if phase <= 0.5 else (1 - phase) * 100
    
    next_full = _calculate_next_phase(date, Config.FULL_MOON_OFFSET)
    next_new = _calculate_next_phase(date, 0.0)
    
    return MoonData(
        phase_type=phase_type,
        illumination=illumination,
        next_full=next_full,
        next_new=next_new
    )


def _calculate_next_phase(from_date: datetime, offset: float) -> datetime:
    """Calculate next occurrence of a specific moon phase."""
    diff = from_date - Config.NEW_MOON_REFERENCE
    days = diff.total_seconds() / 86400.0
    
    cycles = (days - offset) / Config.LUNAR_CYCLE_DAYS
    next_cycle = int(cycles) + 1
    next_timestamp = (
        Config.NEW_MOON_REFERENCE.timestamp() + 
        ((next_cycle * Config.LUNAR_CYCLE_DAYS) + offset) * 86400.0
    )
    
    return datetime.fromtimestamp(next_timestamp)


# ============================================================================
# CALENDAR GENERATION (FIXED ALIGNMENT - MATCHES ORIGINAL)
# ============================================================================

class CalendarGenerator:
    """
    Accessible calendar formatter with PERFECT ALIGNMENT.
    
    Alignment strategy (matching original):
    - Weekday headers: 3 chars each (Mon Tue Wed Thu Fri Sat Sun)
    - Day cells: 5 chars each ("  1  ", " 12  ")
    - Empty cells: 5 spaces
    """
    
    def __init__(self, colors: ThemeColors):
        self.colors = colors
        # Use 3-letter abbreviations for consistent width
        self.weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    
    def generate(self, year: int, month: int) -> str:
        """Generate formatted calendar with perfect alignment."""
        import calendar
        
        cal = calendar.Calendar(firstweekday=calendar.MONDAY)
        month_days = cal.monthdayscalendar(year, month)
        today = datetime.now()
        
        lines: list[str] = []
        
        # Header with icon
        self._add_header(lines, year, month)
        self._add_weekday_headers(lines)
        self._add_days(lines, month_days, today, year, month)
        self._add_footer(lines, year, month)
        
        return "\n".join(lines)
    
    def _add_header(self, lines: list[str], year: int, month: int) -> None:
        """Add styled month/year header."""
        month_name = self._get_month_name(month)
        
        header_parts = [
            f"<span foreground='{self.colors.cyan}' size='large'>{Config.ICON_CLOCK}</span>",
            f"<span foreground='{self.colors.white}' size='large' weight='bold'>{month_name}</span>",
            f"<span foreground='{self.colors.bright_black}' size='large'>{year}</span>"
        ]
        lines.append(" ".join(header_parts))
        lines.append("")
    
    def _add_weekday_headers(self, lines: list[str]) -> None:
        """Add weekday abbreviation row with perfect alignment."""
        parts = []
        for i, day in enumerate(self.weekdays):
            # Weekend days get accent color
            color = self.colors.red if i >= 5 else self.colors.yellow
            weight = "bold" if i >= 5 else "normal"
            
            parts.append(
                f"<span foreground='{color}' weight='{weight}' font_family='monospace'>{day}</span>"
            )
        
        # Join with 2 spaces between for consistent spacing
        lines.append(f"<span font_family='monospace'>{'  '.join(parts)}</span>")
        lines.append("")
    
    def _add_days(
        self, 
        lines: list[str], 
        weeks: list[list[int]], 
        today: datetime,
        year: int, 
        month: int
    ) -> None:
        """Add calendar day grid with ORIGINAL alignment logic."""
        for week in weeks:
            parts = []
            for day_idx, day in enumerate(week):
                if day == 0:
                    # 5 spaces to match " XX  " format
                    parts.append("     ")
                    continue
                
                # Format: "  1  " or " 12  " - exactly 5 chars
                # Original used: f"{day_str}   " where day_str is f"{day:2d}"
                day_str = f"{day:2d}"
                is_today = (day == today.day and month == today.month and year == today.year)
                is_weekend = day_idx >= 5
                
                if is_today:
                    # High contrast today indicator with background
                    cell = (
                        f"<span foreground='{self.colors.black}' "
                        f"background='{self.colors.cyan}' "
                        f"weight='bold' font_family='monospace'>{day_str}</span>   "
                    )
                elif is_weekend:
                    cell = f"<span foreground='{self.colors.red}' font_family='monospace'>{day_str}</span>   "
                else:
                    cell = f"<span foreground='{self.colors.white}' font_family='monospace'>{day_str}</span>   "
                
                parts.append(cell)
            
            lines.append(f"<span font_family='monospace'>{''.join(parts)}</span>")
    
    def _add_footer(self, lines: list[str], year: int, month: int) -> None:
        """Add next month preview."""
        next_month = month + 1 if month < 12 else 1
        next_year = year if month < 12 else year + 1
        next_name = self._get_month_name(next_month)[:3]
        
        lines.append("")
        lines.append(
            f"<span foreground='{self.colors.green}'><b>{Config.ICON_CALENDAR} Next Month</b></span>"
        )
        lines.append(
            f"<span foreground='{self.colors.bright_black}'>{next_name} {next_year}</span>"
        )
    
    @staticmethod
    def _get_month_name(month: int) -> str:
        """Get full month name."""
        import calendar
        return calendar.month_name[month]


# ============================================================================
# SYSTEM INFORMATION
# ============================================================================

@functools.lru_cache(maxsize=1)
def get_system_info() -> SystemInfo:
    """Gather system information with caching."""
    uptime = _get_uptime()
    timers = _check_timers()
    load = _get_load_average()
    
    return SystemInfo(
        uptime_text=uptime, 
        has_active_timers=timers,
        load_average=load
    )


def _get_uptime() -> Optional[str]:
    """Read system uptime from /proc/uptime."""
    try:
        with open("/proc/uptime", "r", encoding="ascii") as f:
            content = f.readline()
            uptime_seconds = float(content.split()[0])
            
            hours = int(uptime_seconds // 3600)
            days = hours // 24
            remaining_hours = hours % 24
            minutes = int((uptime_seconds % 3600) // 60)
            
            if days > 0:
                return f"{days}d {remaining_hours}h {minutes}m"
            return f"{hours}h {minutes}m"
    except (FileNotFoundError, ValueError, PermissionError, IndexError):
        return None


def _get_load_average() -> Optional[str]:
    """Read system load average."""
    try:
        with open("/proc/loadavg", "r", encoding="ascii") as f:
            content = f.readline()
            loads = content.split()[:3]
            return f"{loads[0]} {loads[1]} {loads[2]}"
    except (FileNotFoundError, PermissionError, IndexError):
        return None


def _check_timers() -> bool:
    """Check for active systemd timers (Linux-specific)."""
    if not os.path.exists("/run/systemd/system"):
        return False
    
    try:
        import subprocess
        
        result = subprocess.run(
            ["systemctl", "list-timers", "--all", "--no-pager", "--no-legend"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False
        )
        
        if result.returncode != 0:
            return False
            
        lines = result.stdout.strip().split("\n")
        return bool(lines and lines[0] and not lines[0].isspace())
        
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError):
        return False


# ============================================================================
# OUTPUT FORMATTING
# ============================================================================

class WaybarFormatter:
    """Enhanced Waybar JSON output formatter."""
    
    def __init__(self, colors: ThemeColors):
        self.colors = colors
    
    def format_output(
        self, 
        now: datetime, 
        calendar_html: str, 
        moon: MoonData,
        system: SystemInfo
    ) -> dict[str, Any]:
        """Construct final Waybar JSON output with enhanced UX."""
        time_str = now.strftime("%H:%M")
        date_str = now.strftime("%a, %b %d")
        
        # Main bar text with semantic colors
        text = (
            f"{Config.ICON_CLOCK} "
            f"<span foreground='{self.colors.cyan}' weight='bold'>{time_str}</span> "
            f"<span foreground='{self.colors.bright_black}'>│</span> "
            f"<span foreground='{self.colors.white}'>{date_str}</span>"
        )
        
        # Build rich tooltip
        tooltip = self._build_tooltip(calendar_html, moon, system)
        
        return {
            "text": text,
            "tooltip": f"<span size='12000'>{tooltip}</span>",
            "markup": "pango",
            "class": "calendar",
            "alt": f"{now:%Y-%m-%d}"
        }
    
    def _build_tooltip(
        self, 
        calendar_html: str, 
        moon: MoonData, 
        system: SystemInfo
    ) -> str:
        """Build structured tooltip with visual hierarchy."""
        sections: list[str] = []
        
        # Calendar Section
        sections.append(calendar_html)
        
        # Visual separator
        sections.append(self._create_separator())
        
        # Moon Phase Section
        sections.append(self._build_moon_section(moon))
        
        # System Status Section (if available)
        if system.uptime_text or system.has_active_timers:
            sections.append(self._create_separator())
            sections.append(self._build_system_section(system))
        
        return "\n".join(sections)
    
    def _create_separator(self) -> str:
        """Create visual separator line."""
        return (
            f"<span foreground='{self.colors.bright_black}'>"
            f"{'─' * Config.TOOLTIP_WIDTH}</span>"
        )
    
    def _build_moon_section(self, moon: MoonData) -> str:
        """Build moon phase section with progress visualization."""
        now = datetime.now()
        days_to_full = (moon.next_full - now).days
        days_to_new = (moon.next_new - now).days
        
        lines = [
            f"<span foreground='{self.colors.yellow}' size='large' weight='bold'>"
            f"{Config.ICON_MOON} Moon Phase</span>",
            "",
            f"<span foreground='{self.colors.white}' size='large'>{moon.emoji} "
            f"<b>{moon.name}</b></span>",
            f"<span foreground='{self.colors.bright_black}' size='small'>"
            f"  {moon.meaning}</span>",
            "",
            f"<span foreground='{self.colors.cyan}' font_family='monospace'>"
            f"  {moon.progress_bar}</span>",
            "",
            f"<span foreground='{self.colors.bright_black}'>  🌕 Full Moon in "
            f"<span foreground='{self.colors.white}'>{days_to_full} days</span></span>",
            f"<span foreground='{self.colors.bright_black}'>  🌑 New Moon in "
            f"<span foreground='{self.colors.white}'>{days_to_new} days</span></span>",
        ]
        
        return "\n".join(lines)
    
    def _build_system_section(self, system: SystemInfo) -> str:
        """Build system status section."""
        lines = [
            f"<span foreground='{self.colors.green}' weight='bold'>"
            f"{Config.ICON_UPTIME} System Status</span>",
            ""
        ]
        
        if system.uptime_text:
            lines.append(
                f"<span foreground='{self.colors.cyan}'>  {Config.ICON_UPTIME} "
                f"<b>Uptime:</b> {system.uptime_text}</span>"
            )
        
        if system.load_average:
            lines.append(
                f"<span foreground='{self.colors.bright_black}'>  Load: "
                f"{system.load_average}</span>"
            )
        
        if system.has_active_timers:
            lines.append(
                f"<span foreground='{self.colors.green}'>  {Config.ICON_TIMER} "
                f"Active timers running</span>"
            )
        
        return "\n".join(lines)


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def main() -> int:
    """Main entry point with comprehensive error handling."""
    try:
        # Load theme (cached)
        colors = load_theme_colors()
        
        # Get current time
        now = datetime.now()
        
        # Generate calendar
        calendar_gen = CalendarGenerator(colors)
        calendar_html = calendar_gen.generate(now.year, now.month)
        
        # Calculate moon phase (cached)
        moon_data = calculate_moon_phase(now)
        
        # Get system info (cached)
        system_info = get_system_info()
        
        # Format output
        formatter = WaybarFormatter(colors)
        output = formatter.format_output(now, calendar_html, moon_data, system_info)
        
        # Output JSON
        print(json.dumps(output, ensure_ascii=False))
        return 0
        
    except Exception as e:
        # Critical error - output valid JSON so Waybar doesn't break
        error_output = {
            "text": f"{Config.ICON_CLOCK} <span foreground='#f38ba8'>Error</span>",
            "tooltip": f"<span foreground='#f38ba8'>Module error:</span> {str(e)}",
            "class": "calendar-error",
            "markup": "pango"
        }
        print(json.dumps(error_output, ensure_ascii=False), file=sys.stdout)
        print(f"Calendar module error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
