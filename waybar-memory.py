#!/usr/bin/env python3
"""
Waybar Memory Module - Optimized Version

A high-performance, maintainable memory monitor for Waybar with:
- Intelligent caching for hardware detection (DIMM info rarely changes)
- Single-call psutil data collection
- Proper error handling with specific exceptions
- Modular architecture separating data/presentation
- Type hints for maintainability

Requirements: psutil, python3.9+
Optional: lm_sensors, dmidecode (with sudo), tomllib (Python 3.11+)
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Optional

# Third-party imports with graceful degradation
try:
    import tomllib
except ImportError:
    tomllib = None  # type: ignore

try:
    import psutil
except ImportError as e:
    print(f"Error: psutil is required. Install with: pip install psutil ({e})", file=sys.stderr)
    sys.exit(1)


# =============================================================================
# CONFIGURATION & CONSTANTS
# =============================================================================

@dataclass(frozen=True)
class Config:
    """Immutable configuration constants."""
    MEM_ICON: str = ""
    TOOLTIP_WIDTH: int = 48
    GRAPH_WIDTH: int = 44  # TOOLTIP_WIDTH - 4
    REFRESH_INTERVAL: float = 2.0  # Expected Waybar refresh interval
    
    # File paths
    THEME_PATH: Path = field(default_factory=lambda: Path.home() / ".config/omarchy/current/theme/colors.toml")
    
    # Command timeouts (seconds)
    CMD_TIMEOUT: int = 5
    SUDO_TIMEOUT: int = 30
    
    # Feature flags
    ENABLE_CACHE_CLEAR: bool = True
    ENABLE_DIMM_DETECTION: bool = True
    ENABLE_TEMP_MONITORING: bool = True


CONFIG: Final = Config()


# =============================================================================
# COLOR MANAGEMENT
# =============================================================================

@dataclass(frozen=True)
class ColorTheme:
    """Type-safe color theme container."""
    black: str = "#000000"
    red: str = "#ff0000"
    green: str = "#00ff00"
    yellow: str = "#ffff00"
    blue: str = "#0000ff"
    magenta: str = "#ff00ff"
    cyan: str = "#00ffff"
    white: str = "#ffffff"
    bright_black: str = "#555555"
    bright_red: str = "#ff5555"
    bright_green: str = "#55ff55"
    bright_yellow: str = "#ffff55"
    bright_blue: str = "#5555ff"
    bright_magenta: str = "#ff55ff"
    bright_cyan: str = "#55ffff"
    bright_white: str = "#ffffff"
    
    @classmethod
    def from_omarchy_toml(cls, path: Path) -> ColorTheme:
        """Load Omarchy theme from TOML file."""
        defaults = cls()
        
        if not tomllib or not path.exists():
            return defaults
            
        try:
            content = path.read_text(encoding="utf-8")
            data = tomllib.loads(content)
            
            # Map Omarchy's color0-15 to semantic names
            return cls(
                black=data.get("color0", defaults.black),
                red=data.get("color1", defaults.red),
                green=data.get("color2", defaults.green),
                yellow=data.get("color3", defaults.yellow),
                blue=data.get("color4", defaults.blue),
                magenta=data.get("color5", defaults.magenta),
                cyan=data.get("color6", defaults.cyan),
                white=data.get("color7", defaults.white),
                bright_black=data.get("color8", defaults.bright_black),
                bright_red=data.get("color9", defaults.bright_red),
                bright_green=data.get("color10", defaults.bright_green),
                bright_yellow=data.get("color11", defaults.bright_yellow),
                bright_blue=data.get("color12", defaults.bright_blue),
                bright_magenta=data.get("color13", defaults.bright_magenta),
                bright_cyan=data.get("color14", defaults.bright_cyan),
                bright_white=data.get("color15", defaults.bright_white),
            )
        except (OSError, ValueError, KeyError) as e:
            print(f"Warning: Failed to load theme ({e}), using defaults", file=sys.stderr)
            return defaults


# Lazy-loaded singleton
_theme_instance: Optional[ColorTheme] = None

def get_theme() -> ColorTheme:
    """Get cached color theme (lazy loading)."""
    global _theme_instance
    if _theme_instance is None:
        _theme_instance = ColorTheme.from_omarchy_toml(CONFIG.THEME_PATH)
    return _theme_instance


# =============================================================================
# COLOR LOGIC
# =============================================================================

@dataclass(frozen=True)
class ColorThreshold:
    """Color threshold definition."""
    color: str
    min_val: float
    max_val: float


class ColorScale:
    """Manages color scales for different metrics."""
    
    def __init__(self, theme: ColorTheme):
        self.theme = theme
        self._storage_scale = [
            ColorThreshold(theme.blue, 0.0, 10.0),
            ColorThreshold(theme.cyan, 10.0, 20.0),
            ColorThreshold(theme.green, 20.0, 40.0),
            ColorThreshold(theme.yellow, 40.0, 60.0),
            ColorThreshold(theme.bright_yellow, 60.0, 80.0),
            ColorThreshold(theme.bright_red, 80.0, 90.0),
            ColorThreshold(theme.red, 90.0, 100.0),
        ]
        self._temp_scale = [
            ColorThreshold(theme.blue, 0, 40),
            ColorThreshold(theme.cyan, 41, 50),
            ColorThreshold(theme.green, 51, 60),
            ColorThreshold(theme.yellow, 61, 70),
            ColorThreshold(theme.bright_yellow, 71, 75),
            ColorThreshold(theme.bright_red, 76, 80),
            ColorThreshold(theme.red, 81, 999),
        ]
    
    def get_color(self, value: Optional[float], metric_type: str) -> str:
        """Get color for value based on metric type."""
        if value is None:
            return self.theme.white
            
        try:
            val = float(value)
        except (ValueError, TypeError):
            return self.theme.white
        
        scale = self._storage_scale if metric_type == "mem_storage" else self._temp_scale
        
        for threshold in scale:
            if threshold.min_val <= val <= threshold.max_val:
                return threshold.color
        return scale[-1].color


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class MemoryModule:
    """Represents a physical memory module (DIMM)."""
    label: str = "Unknown"
    size: str = "N/A"
    type: str = "DDR4"
    speed: str = "N/A"
    temp: int = 0


@dataclass  
class MemoryStats:
    """Consolidated memory statistics."""
    total_gb: float = 0.0
    used_gb: float = 0.0
    available_gb: float = 0.0
    cached_gb: float = 0.0
    buffers_gb: float = 0.0
    percent: float = 0.0
    
    @property
    def used_pct(self) -> float:
        if self.total_gb == 0:
            return 0.0
        return (self.used_gb / self.total_gb) * 100
    
    @property
    def cached_pct(self) -> float:
        if self.total_gb == 0:
            return 0.0
        return (self.cached_gb / self.total_gb) * 100
        
    @property
    def buffers_pct(self) -> float:
        if self.total_gb == 0:
            return 0.0
        return (self.buffers_gb / self.total_gb) * 100
    
    @property
    def free_pct(self) -> float:
        return max(0.0, 100.0 - self.used_pct - self.cached_pct - self.buffers_pct)


# =============================================================================
# CACHED HARDWARE DETECTION
# =============================================================================

@functools.lru_cache(maxsize=1)
def get_memory_modules() -> tuple[MemoryModule, ...]:
    """
    Fetch memory module info via dmidecode (cached).
    
    Hardware configuration rarely changes, so cache indefinitely.
    Cache is per-process (Waybar restart clears it).
    """
    if not CONFIG.ENABLE_DIMM_DETECTION:
        return ()
        
    if not shutil.which("dmidecode"):
        return ()
    
    try:
        result = subprocess.run(
            ["sudo", "-n", "/usr/sbin/dmidecode", "--type", "memory"],
            capture_output=True,
            text=True,
            timeout=CONFIG.CMD_TIMEOUT,
            check=False
        )
        
        if result.returncode != 0:
            return ()
            
        return tuple(_parse_dmidecode_output(result.stdout))
        
    except (subprocess.TimeoutExpired, OSError):
        return ()


def _parse_dmidecode_output(output: str) -> list[MemoryModule]:
    """Parse dmidecode text output into MemoryModule objects."""
    modules = []
    current: dict[str, Any] = {}
    
    # Pre-fetch temperatures (may be empty if sensors unavailable)
    temps = _get_memory_temps()
    temp_idx = 0
    
    for line in output.splitlines():
        line = line.strip()
        
        if line.startswith("Memory Device"):
            if current and _is_valid_module(current):
                modules.append(_create_module(current, temps, temp_idx))
                temp_idx += 1
            current = {}
        elif line.startswith("Locator:"):
            current["label"] = line.split(":", 1)[1].strip()
        elif line.startswith("Size:"):
            current["size"] = _normalize_size(line.split(":", 1)[1].strip())
        elif line.startswith("Type:"):
            current["type"] = line.split(":", 1)[1].strip()
        elif line.startswith("Speed:"):
            current["speed"] = _normalize_speed(line.split(":", 1)[1].strip())
    
    # Handle last module
    if current and _is_valid_module(current):
        modules.append(_create_module(current, temps, temp_idx))
    
    return modules


def _is_valid_module(data: dict[str, Any]) -> bool:
    """Check if parsed data represents an actual memory module."""
    size = data.get("size", "")
    return size and size != "No Module Installed" and size != "0 MB"


def _create_module(data: dict[str, Any], temps: list[int], idx: int) -> MemoryModule:
    """Create MemoryModule from parsed data."""
    temp = temps[idx] if idx < len(temps) else 0
    return MemoryModule(
        label=data.get("label", "DIMM"),
        size=data.get("size", "N/A"),
        type=data.get("type", "DDR4"),
        speed=data.get("speed", "N/A"),
        temp=temp
    )


def _normalize_size(size_str: str) -> str:
    """Convert size string to standardized format."""
    if "MB" in size_str:
        try:
            mb = int(size_str.replace("MB", "").strip())
            if mb >= 1024:
                return f"{mb // 1024} GB"
        except ValueError:
            pass
    return size_str


def _normalize_speed(speed_str: str) -> str:
    """Normalize speed string (MT/s -> MHz)."""
    return speed_str.replace("MT/s", "MHz") if "MT/s" in speed_str else speed_str


@functools.lru_cache(maxsize=1)
def _get_memory_temps() -> tuple[int, ...]:
    """
    Read memory temperatures from lm_sensors (cached).
    
    Looks for jc42, spd, or dram temperature sensors [^1^].
    Returns tuple for immutability (cacheable).
    """
    if not CONFIG.ENABLE_TEMP_MONITORING:
        return ()
        
    if not shutil.which("sensors"):
        return ()
    
    try:
        result = subprocess.run(
            ["sensors", "-j"],
            capture_output=True,
            text=True,
            timeout=CONFIG.CMD_TIMEOUT,
            check=False
        )
        
        if result.returncode != 0:
            return ()
        
        data = json.loads(result.stdout)
        temps = []
        
        for chip_name, chip_data in data.items():
            # Look for memory-related temperature chips
            if any(x in chip_name.lower() for x in ["jc42", "spd", "dram"]):
                temps.extend(_extract_temps_from_chip(chip_data))
        
        return tuple(temps)
        
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return ()


def _extract_temps_from_chip(chip_data: dict[str, Any]) -> list[int]:
    """Extract temperature values from sensor chip data."""
    temps = []
    for feature, subfeatures in chip_data.items():
        if isinstance(subfeatures, dict):
            for key, value in subfeatures.items():
                if "input" in key and isinstance(value, (int, float)):
                    temps.append(int(value))
    return temps


# =============================================================================
# MEMORY STATS COLLECTION
# =============================================================================

def get_memory_stats() -> MemoryStats:
    """Get current memory statistics (single psutil call)."""
    try:
        mem = psutil.virtual_memory()
        
        # Safely handle missing attributes (older psutil versions)
        cached = getattr(mem, "cached", 0)
        buffers = getattr(mem, "buffers", 0)
        
        return MemoryStats(
            total_gb=mem.total / (1024**3),
            used_gb=mem.used / (1024**3),
            available_gb=mem.available / (1024**3),
            cached_gb=cached / (1024**3),
            buffers_gb=buffers / (1024**3),
            percent=mem.percent
        )
    except (OSError, AttributeError) as e:
        print(f"Error reading memory stats: {e}", file=sys.stderr)
        return MemoryStats()


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

# Pre-compiled regex for performance
_PANGO_TAG_RE: Final = re.compile(r"<span[^>]*>|</span>|<[^>]+>")

def strip_pango_tags(text: str) -> str:
    """Remove Pango markup tags for width calculation."""
    return _PANGO_TAG_RE.sub("", text)


def visible_len(text: str) -> int:
    """Calculate visible text length excluding Pango tags."""
    return len(strip_pango_tags(text))


def center_line(line: str, width: int = CONFIG.TOOLTIP_WIDTH - 2, pad_char: str = " ") -> str:
    """Center text with Pango markup support."""
    vlen = visible_len(line)
    if vlen >= width:
        return line
    left_pad = (width - vlen) // 2
    right_pad = width - vlen - left_pad
    return f"{pad_char * left_pad}{line}{pad_char * right_pad}"


def left_line(line: str, width: int = CONFIG.TOOLTIP_WIDTH - 2, pad_char: str = " ") -> str:
    """Left-align text with Pango markup support."""
    vlen = visible_len(line)
    if vlen >= width:
        return line
    return f"{line}{pad_char * (width - vlen)}"


def send_notification(title: str, message: str, urgency: str = "normal") -> None:
    """Send desktop notification via notify-send."""
    if not shutil.which("notify-send"):
        return
        
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-t", "5000", title, message],
            capture_output=True,
            check=False,
            timeout=5
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


# =============================================================================
# CACHE CLEARING
# =============================================================================

def clear_ram_cache() -> None:
    """Clear RAM cache with proper error handling and user feedback."""
    if not CONFIG.ENABLE_CACHE_CLEAR:
        send_notification("Cache Clear Disabled", "Feature not enabled in config", "normal")
        return
    
    # Check if we're on Linux with drop_caches support
    drop_caches_path = Path("/proc/sys/vm/drop_caches")
    if not drop_caches_path.exists():
        send_notification("❌ Not Supported", "Cache clearing only available on Linux", "critical")
        return
    
    try:
        # Get before stats
        mem_before = psutil.virtual_memory()
        cached_before = getattr(mem_before, "cached", 0) / (1024**3)
        
        # Run sync first (ensures data is written to disk)
        sync_result = subprocess.run(
            ["sudo", "-n", "/usr/bin/sync"],
            capture_output=True,
            text=True,
            timeout=CONFIG.SUDO_TIMEOUT,
            check=False
        )
        
        if sync_result.returncode != 0:
            _handle_sudo_error(sync_result.stderr)
            return
        
        # Drop caches
        result = subprocess.run(
            ["sudo", "-n", "/usr/bin/sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
            capture_output=True,
            text=True,
            timeout=CONFIG.SUDO_TIMEOUT,
            check=False
        )
        
        if result.returncode == 0:
            # Get after stats
            mem_after = psutil.virtual_memory()
            cached_after = getattr(mem_after, "cached", 0) / (1024**3)
            cleared = max(0.0, cached_before - cached_after)
            
            send_notification(
                "✅ RAM Cache Cleared",
                f"Freed {cleared:.2f} GB of cache\n"
                f"Cached: {cached_before:.2f} GB → {cached_after:.2f} GB",
                "normal"
            )
        else:
            _handle_sudo_error(result.stderr)
            
    except subprocess.TimeoutExpired:
        send_notification("❌ Cache Clear Failed", "Operation timed out", "critical")
    except Exception as e:
        send_notification("❌ Cache Clear Error", str(e), "critical")


def _handle_sudo_error(stderr: str) -> None:
    """Handle sudo-specific errors with helpful messages."""
    error_lower = stderr.lower()
    
    if "password" in error_lower or "sorry" in error_lower:
        # Get current username for accurate sudoers example
        user = os.getenv("USER", "username")
        send_notification(
            "❌ Cache Clear Failed",
            f"Sudo password required.\n\n"
            f"Configure NOPASSWD:\n"
            f"sudo visudo -f /etc/sudoers.d/waybar-cache-clear\n\n"
            f"Add:\n"
            f"{user} ALL=(root) NOPASSWD: /usr/bin/sync\n"
            f"{user} ALL=(root) NOPASSWD: /usr/bin/sh",
            "critical"
        )
    else:
        error_msg = stderr.strip() if stderr else "Permission denied"
        send_notification("❌ Cache Clear Failed", f"Error: {error_msg}", "critical")


# =============================================================================
# OUTPUT GENERATION
# =============================================================================

class TooltipBuilder:
    """Builds Waybar tooltip with memory visualization."""
    
    def __init__(self, theme: ColorTheme, colors: ColorScale):
        self.theme = theme
        self.colors = colors
        self.lines: list[str] = []
    
    def build(self, stats: MemoryStats, modules: tuple[MemoryModule, ...]) -> str:
        """Construct full tooltip HTML."""
        self.lines = []
        
        self._add_header()
        self._add_modules(modules)
        self._add_visualization(stats)
        self._add_legend(stats)
        self._add_footer()
        
        return "<span size='12000'>" + "\n".join(self.lines) + "</span>"
    
    def _add_header(self) -> None:
        """Add tooltip header with icon."""
        icon = f"<span size='large' foreground='{self.theme.green}'>{CONFIG.MEM_ICON}</span>"
        text = f"<span size='large' foreground='{self.theme.white}'>Memory</span>"
        self.lines.append(f"{icon} {text}")
        self.lines.append(f"<span foreground='{self.theme.bright_black}'>{'─' * CONFIG.TOOLTIP_WIDTH}</span>")
    
    def _add_modules(self, modules: tuple[MemoryModule, ...]) -> None:
        """Add memory module table if available."""
        if not modules:
            return
            
        for mod in modules:
            temp_color = self.colors.get_color(float(mod.temp), "mem_temp")
            temp_str = f"<span foreground='{temp_color}'>[{mod.temp}°C]</span>"
            
            line = (
                f"{mod.label:<7} │ "
                f"{mod.size:<7} │ "
                f"{mod.type:<5} │ "
                f"{mod.speed:<6} "
                f"{temp_str}"
            )
            self.lines.append(left_line(line))
        
        self.lines.append("")
    
    def _add_visualization(self, stats: MemoryStats) -> None:
        """Add ASCII bar chart visualization."""
        # Calculate dimensions
        inner_width = CONFIG.GRAPH_WIDTH - 4
        bar_len = inner_width - 2
        
        # Calculate bar segments
        used_len = int((stats.used_pct / 100.0) * bar_len)
        cached_len = int((stats.cached_pct / 100.0) * bar_len)
        buffers_len = int((stats.buffers_pct / 100.0) * bar_len)
        free_len = max(0, bar_len - used_len - cached_len - buffers_len)
        
        # Get connector color based on max module temp
        modules = get_memory_modules()
        max_temp = max((m.temp for m in modules), default=0)
        connector_color = self.colors.get_color(float(max_temp), "mem_temp")
        frame_color = self.theme.white
        
        # Center padding
        padding = " " * ((CONFIG.TOOLTIP_WIDTH - CONFIG.GRAPH_WIDTH) // 2)
        
        def c(text: str, color: str) -> str:
            return f"<span foreground='{color}'>{text}</span>"
        
        # Build ASCII art lines
        self.lines.append(f"{padding} {c('╭' + '─'*inner_width + '╮', frame_color)}")
        self.lines.append(f"{padding}{c('╭╯', frame_color)}{c('░'*inner_width, connector_color)}{c('╰╮', frame_color)}")
        
        # Bar line
        bar = (
            f"{c('█' * used_len, self.theme.red)}"
            f"{c('█' * cached_len, self.theme.yellow)}"
            f"{c('█' * buffers_len, self.theme.cyan)}"
            f"{c('█' * free_len, self.theme.bright_black)}"
        )
        self.lines.append(f"{padding}{c('╰╮', frame_color)}{c('░', connector_color)}{bar}{c('░', connector_color)}{c('╭╯', frame_color)}")
        
        # Frame bottom
        self.lines.append(f"{padding} {c('│', frame_color)}{c('░'*inner_width, connector_color)}{c('│', frame_color)}")
        self.lines.append(f"{padding}{c('╭╯', frame_color)}{c('┌' + '┬'*bar_len + '┐', frame_color)}{c('╰╮', frame_color)}")
        self.lines.append(f"{padding}{c('└─', frame_color)}{c('┴'*inner_width, frame_color)}{c('─┘', frame_color)}")
        self.lines.append("")
    
    def _add_legend(self, stats: MemoryStats) -> None:
        """Add color legend with percentages."""
        # Line 1: Used and Cached
        line1 = (
            f"<span size='11000'>"
            f"<span foreground='{self.theme.red}'>Used</span> {stats.used_pct:4.1f}%"
            f"            "
            f"<span foreground='{self.theme.yellow}'>Cached</span> {stats.cached_pct:4.1f}%"
            f"</span>"
        )
        self.lines.append(center_line(line1))
        
        # Line 2: Buffers and Free
        line2 = (
            f"<span size='11000'>"
            f"<span foreground='{self.theme.cyan}'>Buffers</span> {stats.buffers_pct:4.1f}%"
            f"            "
            f"<span foreground='{self.theme.bright_black}'>Free</span> {stats.free_pct:4.1f}%"
            f"</span>"
        )
        self.lines.append(center_line(line2))

    def _add_footer(self) -> None:
        """Add action hint footer."""
        # Calculate width based on the configured tooltip width
        # Subtracting 2-4 accounts for padding and prevents wrapping
        width = CONFIG.TOOLTIP_WIDTH - 4
        separator = "─" * width
        
        self.lines.append("")
        # Ensure the separator is treated as a single line and matches the theme
        self.lines.append(f"<span foreground='{self.theme.bright_black}'>{center_line(separator)}</span>")
        
        hint = "󰍽 LMB: Clear RAM Cache"
        # Using 11000 or 10000 (roughly 10-11pt) is often safer for hints
        self.lines.append(center_line(f"<span size='11000'>{hint}</span>"))


def generate_waybar_output() -> dict[str, Any]:
    """Generate complete Waybar JSON output."""
    # Collect data
    stats = get_memory_stats()
    modules = get_memory_modules()
    
    # Initialize styling
    theme = get_theme()
    colors = ColorScale(theme)
    
    # Build text (icon + percentage)
    color = colors.get_color(stats.percent, "mem_storage")
    text = f"{CONFIG.MEM_ICON} <span foreground='{color}'>{int(stats.percent)}%</span>"
    
    # Build tooltip
    builder = TooltipBuilder(theme, colors)
    tooltip = builder.build(stats, modules)
    
    return {
        "text": text,
        "tooltip": tooltip,
        "markup": "pango",
        "class": "memory",
    }


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def main() -> None:
    """Main entry point with argument parsing."""
    parser = argparse.ArgumentParser(
        description="Waybar Memory Module - Optimized system memory monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Output Waybar JSON
  %(prog)s --clear-cache      # Clear RAM cache
  %(prog)s --show-modules     # Display detected memory modules
        """
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear RAM cache and show notification"
    )
    parser.add_argument(
        "--show-modules",
        action="store_true",
        help="Display detected memory modules and exit"
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 2.0.0-optimized"
    )
    
    args = parser.parse_args()
    
    if args.clear_cache:
        clear_ram_cache()
    elif args.show_modules:
        modules = get_memory_modules()
        if modules:
            print("Detected Memory Modules:")
            for i, mod in enumerate(modules, 1):
                print(f"  {i}. {mod.label}: {mod.size} {mod.type} @ {mod.speed} ({mod.temp}°C)")
        else:
            print("No memory modules detected (dmidecode may require sudo)")
    else:
        output = generate_waybar_output()
        print(json.dumps(output))


if __name__ == "__main__":
    main()
