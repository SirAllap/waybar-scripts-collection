#!/usr/bin/env python3
"""
WAYBAR GPU MODULE - AMD Edition (Optimized Refactored Version)
High-performance system monitoring with caching, proper error handling, and modularity.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from bisect import bisect_right
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, ClassVar, Optional, TypeVar, Union

# Optional dependency handling
try:
    import tomllib
except ImportError:
    tomllib = None


# ============================================================================
# CONFIGURATION & CONSTANTS
# ============================================================================

class Config:
    """Centralized configuration with type safety."""
    GPU_ICON: str = "󰢮"
    TOOLTIP_WIDTH: int = 35
    UPDATE_INTERVAL: float = 1.0  # seconds
    
    # GPU identification
    GPU_PCI_IDS: ClassVar[dict[str, str]] = {
        "0x73bf": "AMD Radeon RX 6800",
        "0x73df": "AMD Radeon RX 6800 XT",
        "0x73af": "AMD Radeon RX 6900 XT",
        "0x744c": "AMD Radeon RX 7900 XTX",
        "0x7480": "AMD Radeon RX 7600",
    }
    
    # Sysfs paths
    DRM_BASE: Path = Path("/sys/class/drm")
    DEFAULT_FAN_MAX: int = 3300
    DEFAULT_TDP: float = 250.0
    
    # Process detection
    GPU_PROCESS_NAMES: frozenset[str] = frozenset({
        'chrome', 'chromium', 'firefox', 'zen', 'steam', 'proton', 
        'wine', 'vkcube', 'glxgears', 'obs', 'kdenlive', 'blender',
        'gamescope', 'mangohud'
    })


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True, slots=True)
class ColorThreshold:
    """Immutable color threshold configuration."""
    color: str
    temp_min: int = 0
    temp_max: int = 999
    power_min: float = 0.0
    power_max: float = 999.0
    
    def matches_temp(self, temp: int) -> bool:
        return self.temp_min <= temp <= self.temp_max
    
    def matches_power(self, power: float) -> bool:
        return self.power_min <= power <= self.power_max


@dataclass(slots=True)
class GPUStats:
    """Container for GPU metrics with validation."""
    name: str = "AMD GPU"
    temperature: int = 0
    utilization: int = 0
    power_draw: float = 0.0
    power_limit: float = Config.DEFAULT_TDP
    vram_used: int = 0
    vram_total: int = 0
    fan_rpm: int = 0
    fan_percent: float = 0.0
    device_path: Optional[Path] = None
    
    @property
    def vram_percent(self) -> float:
        return (self.vram_used / self.vram_total * 100) if self.vram_total > 0 else 0.0
    
    @property
    def power_percent(self) -> float:
        return (self.power_draw / self.power_limit * 100) if self.power_limit > 0 else 0.0
    
    def is_valid(self) -> bool:
        """Check if stats represent a valid GPU reading."""
        return self.device_path is not None and self.vram_total > 0


@dataclass(slots=True)
class ProcessInfo:
    """Lightweight process information."""
    pid: int
    name: str
    memory_mb: int


# ============================================================================
# THEME MANAGEMENT
# ============================================================================

class ThemeManager:
    """Efficient theme loading with caching."""
    
    _cache: ClassVar[Optional[dict[str, str]]] = None
    _DEFAULT_COLORS: ClassVar[dict[str, str]] = {
        "black": "#000000", "red": "#ff0000", "green": "#00ff00",
        "yellow": "#ffff00", "blue": "#0000ff", "magenta": "#ff00ff",
        "cyan": "#00ffff", "white": "#ffffff", "bright_black": "#555555",
        "bright_red": "#ff5555", "bright_green": "#55ff55",
        "bright_yellow": "#ffff55", "bright_blue": "#5555ff",
        "bright_magenta": "#ff55ff", "bright_cyan": "#55ffff",
        "bright_white": "#ffffff"
    }
    
    @classmethod
    def load(cls, force_reload: bool = False) -> dict[str, str]:
        """Load theme colors with caching."""
        if cls._cache is not None and not force_reload:
            return cls._cache
        
        theme_path = Path.home() / ".config/omarchy/current/theme/colors.toml"
        
        if not tomllib or not theme_path.exists():
            cls._cache = cls._DEFAULT_COLORS.copy()
            return cls._cache
        
        try:
            data = tomllib.loads(theme_path.read_text())
            colors = {
                "black": data.get("color0", "#000000"),
                "red": data.get("color1", "#ff0000"),
                "green": data.get("color2", "#00ff00"),
                "yellow": data.get("color3", "#ffff00"),
                "blue": data.get("color4", "#0000ff"),
                "magenta": data.get("color5", "#ff00ff"),
                "cyan": data.get("color6", "#00ffff"),
                "white": data.get("color7", "#ffffff"),
                "bright_black": data.get("color8", "#555555"),
                "bright_red": data.get("color9", "#ff5555"),
                "bright_green": data.get("color10", "#55ff55"),
                "bright_yellow": data.get("color11", "#ffff55"),
                "bright_blue": data.get("color12", "#5555ff"),
                "bright_magenta": data.get("color13", "#ff55ff"),
                "bright_cyan": data.get("color14", "#55ffff"),
                "bright_white": data.get("color15", "#ffffff"),
            }
            cls._cache = {**cls._DEFAULT_COLORS, **colors}
        except Exception as e:
            # Log error in real implementation; silently fallback here
            cls._cache = cls._DEFAULT_COLORS.copy()
        
        return cls._cache


# ============================================================================
# COLOR MANAGEMENT (Optimized with bisect)
# ============================================================================

class ColorManager:
    """High-performance color lookup using binary search."""
    
    _TEMP_THRESHOLDS: ClassVar[list[int]] = [0, 36, 46, 55, 66, 76, 86, 999]
    _POWER_THRESHOLDS: ClassVar[list[float]] = [0.0, 21.0, 41.0, 61.0, 76.0, 86.0, 96.0, 999.0]
    
    def __init__(self, colors: dict[str, str]):
        self._colors = colors
        self._temp_colors = [
            colors["blue"], colors["cyan"], colors["green"], colors["yellow"],
            colors["bright_yellow"], colors["bright_red"], colors["red"]
        ]
        self._power_colors = self._temp_colors.copy()  # Same gradient
    
    def get_temp_color(self, temp: Union[int, float]) -> str:
        """O(log n) color lookup for temperature."""
        try:
            temp_val = int(temp)
            idx = bisect_right(self._TEMP_THRESHOLDS, temp_val) - 1
            return self._temp_colors[max(0, min(idx, len(self._temp_colors) - 1))]
        except (TypeError, ValueError):
            return self._colors["white"]
    
    def get_power_color(self, power: Union[int, float]) -> str:
        """O(log n) color lookup for power."""
        try:
            power_val = float(power)
            idx = bisect_right(self._POWER_THRESHOLDS, power_val) - 1
            return self._power_colors[max(0, min(idx, len(self._power_colors) - 1))]
        except (TypeError, ValueError):
            return self._colors["white"]


# ============================================================================
# GPU DATA COLLECTOR (With Caching)
# ============================================================================

class GPUCollector:
    """Efficient GPU data collection with path caching."""
    
    def __init__(self):
        self._drm_path: Optional[Path] = None
        self._hwmon_path: Optional[Path] = None
        self._gpu_name: Optional[str] = None
        self._initialized: bool = False
    
    def _find_drm_device(self) -> Optional[Path]:
        """Find AMD GPU device path with caching."""
        if self._drm_path:
            return self._drm_path
        
        # Check card0 through card3 for AMD devices
        for card_num in range(4):
            card_path = Config.DRM_BASE / f"card{card_num}/device"
            if not card_path.exists():
                continue
            
            # Verify it's an AMD GPU by checking vendor
            vendor_path = card_path / "vendor"
            if vendor_path.exists():
                try:
                    vendor = vendor_path.read_text().strip()
                    if vendor in ["0x1002", "0x1022"]:  # AMD PCI vendor IDs
                        self._drm_path = card_path
                        return card_path
                except (IOError, OSError):
                    continue
            
            # Fallback: check for mem_info_vram_total
            if (card_path / "mem_info_vram_total").exists():
                self._drm_path = card_path
                return card_path
        
        return None
    
    def _get_hwmon_path(self) -> Optional[Path]:
        """Cache hwmon path to avoid repeated directory listings."""
        if self._hwmon_path:
            return self._hwmon_path
        
        drm_path = self._find_drm_device()
        if not drm_path:
            return None
        
        hwmon_base = drm_path / "hwmon"
        if not hwmon_base.exists():
            return None
        
        try:
            hwmon_dirs = [d for d in hwmon_base.iterdir() if d.name.startswith("hwmon")]
            if hwmon_dirs:
                self._hwmon_path = hwmon_dirs[0]
                return self._hwmon_path
        except (IOError, OSError):
            pass
        
        return None
    
    def _read_int(self, path: Path, divisor: int = 1, default: int = 0) -> int:
        """Safe integer reading from sysfs."""
        try:
            return int(path.read_text().strip()) // divisor
        except (IOError, OSError, ValueError):
            return default
    
    def _read_float(self, path: Path, divisor: float = 1.0, default: float = 0.0) -> float:
        """Safe float reading from sysfs."""
        try:
            return float(path.read_text().strip()) / divisor
        except (IOError, OSError, ValueError):
            return default
    
    def _identify_gpu(self, device_path: Path) -> str:
        """Identify GPU model with caching."""
        if self._gpu_name:
            return self._gpu_name
        
        device_file = device_path / "device"
        if device_file.exists():
            try:
                device_id = device_file.read_text().strip()
                for pci_id, name in Config.GPU_PCI_IDS.items():
                    if pci_id in device_id:
                        self._gpu_name = name
                        return name
            except (IOError, OSError):
                pass
        
        # Try subsystem_device for more specific identification
        subsystem_file = device_path / "subsystem_device"
        if subsystem_file.exists():
            try:
                sub_id = subsystem_file.read_text().strip()
                if sub_id in Config.GPU_PCI_IDS:
                    self._gpu_name = Config.GPU_PCI_IDS[sub_id]
                    return self._gpu_name
            except (IOError, OSError):
                pass
        
        self._gpu_name = "AMD Radeon GPU"
        return self._gpu_name
    
    def _read_temperature(self, hwmon: Path, drm: Path) -> int:
        """Read temperature with multiple fallback strategies."""
        # Strategy 1: hwmon temp1_input
        temp_path = hwmon / "temp1_input"
        if temp_path.exists():
            val = self._read_int(temp_path, divisor=1000)
            if val > 0:
                return val
        
        # Strategy 2: hwmon temp2_input (edge/junction)
        for temp_file in ["temp2_input", "temp3_input"]:
            temp_path = hwmon / temp_file
            if temp_path.exists():
                val = self._read_int(temp_path, divisor=1000)
                if val > 0:
                    return val
        
        # Strategy 3: sensors command (slow fallback)
        try:
            result = subprocess.run(
                ["sensors"], capture_output=True, text=True, timeout=2
            )
            for line in result.stdout.splitlines():
                if "edge" in line.lower() or "junction" in line.lower():
                    match = re.search(r'\+?([\d.]+)', line)
                    if match:
                        return int(float(match.group(1)))
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        
        return 0
    
    def _read_power(self, hwmon: Path) -> float:
        """Read power consumption with fallback strategies."""
        # Strategy 1: power1_average (microwatts)
        power_path = hwmon / "power1_average"
        if power_path.exists():
            return self._read_float(power_path, divisor=1000000.0)
        
        # Strategy 2: power1_input (instantaneous)
        power_path = hwmon / "power1_input"
        if power_path.exists():
            return self._read_float(power_path, divisor=1000000.0)
        
        # Strategy 3: hwmon power control
        power_path = hwmon / "power1_cap"
        if power_path.exists():
            # Return 0 to indicate we have cap but no reading
            return 0.0
        
        return 0.0
    
    def _read_fan(self, hwmon: Path) -> tuple[int, float]:
        """Read fan RPM and calculate percentage."""
        fan_input = hwmon / "fan1_input"
        fan_max = hwmon / "fan1_max"
        pwm = hwmon / "pwm1"
        pwm_max = hwmon / "pwm1_max"
        
        rpm = self._read_int(fan_input) if fan_input.exists() else 0
        
        # Prefer PWM for percentage (matches CoreCtrl behavior)
        if pwm.exists():
            pwm_val = self._read_int(pwm)
            pwm_max_val = self._read_int(pwm_max, default=255) if pwm_max.exists() else 255
            if pwm_max_val > 0:
                return rpm, (pwm_val / pwm_max_val * 100)
        
        # Fallback to RPM percentage
        if fan_max.exists():
            max_rpm = self._read_int(fan_max)
            if max_rpm > 0 and rpm > 0:
                return rpm, (rpm / max_rpm * 100)
        
        return rpm, 0.0
    
    def collect(self) -> GPUStats:
        """Collect all GPU metrics efficiently."""
        stats = GPUStats()
        
        drm_path = self._find_drm_device()
        if not drm_path:
            return stats  # Return empty stats if no GPU found
        
        stats.device_path = drm_path
        stats.name = self._identify_gpu(drm_path)
        hwmon = self._get_hwmon_path()
        
        # Read utilization
        busy_path = drm_path / "gpu_busy_percent"
        stats.utilization = self._read_int(busy_path)
        
        # Read VRAM
        vram_used_path = drm_path / "mem_info_vram_used"
        vram_total_path = drm_path / "mem_info_vram_total"
        
        if vram_total_path.exists():
            stats.vram_total = self._read_int(vram_total_path, divisor=1024*1024)
            stats.vram_used = self._read_int(vram_used_path, divisor=1024*1024)
        else:
            # Fallback: try to detect from marketing name or assume 16GB
            stats.vram_total = 16384
        
        # Read temperature (hwmon or fallback)
        if hwmon:
            stats.temperature = self._read_temperature(hwmon, drm_path)
            stats.power_draw = self._read_power(hwmon)
            stats.fan_rpm, stats.fan_percent = self._read_fan(hwmon)
            
            # Read power cap if available
            cap_path = hwmon / "power1_cap"
            if cap_path.exists():
                stats.power_limit = self._read_float(cap_path, divisor=1000000.0, 
                                                    default=Config.DEFAULT_TDP)
        
        return stats


# ============================================================================
# PROCESS DETECTION (Optimized Alternative to psutil)
# ============================================================================

class ProcessDetector:
    """Lightweight process detection without full psutil scan."""
    
    @staticmethod
    def find_gpu_processes(max_results: int = 3) -> list[ProcessInfo]:
        """
        Find likely GPU processes by examining /proc directly.
        Much faster than psutil.process_iter() which scans all processes [^5^][^9^].
        """
        processes: list[ProcessInfo] = []
        
        try:
            for pid_str in os.listdir("/proc"):
                if not pid_str.isdigit():
                    continue
                
                pid = int(pid_str)
                proc_dir = Path(f"/proc/{pid}")
                
                # Read command line
                try:
                    cmdline = (proc_dir / "cmdline").read_text().split('\0')
                    if not cmdline or not cmdline[0]:
                        continue
                    
                    exe_name = os.path.basename(cmdline[0]).lower()
                    
                    # Check if it's a GPU process
                    if not any(gpu_proc in exe_name for gpu_proc in Config.GPU_PROCESS_NAMES):
                        continue
                    
                    # Read memory usage
                    status_file = proc_dir / "status"
                    mem_mb = 0
                    if status_file.exists():
                        for line in status_file.read_text().splitlines():
                            if line.startswith("VmRSS:"):
                                # Parse "VmRSS:   123456 kB"
                                parts = line.split()
                                if len(parts) >= 2:
                                    mem_mb = int(parts[1]) // 1024
                                break
                    
                    processes.append(ProcessInfo(pid, exe_name, mem_mb))
                    
                except (IOError, OSError, PermissionError):
                    continue
                
                if len(processes) >= max_results * 2:  # Collect extra for sorting
                    break
        
        except (IOError, OSError):
            pass
        
        # Sort by memory usage and return top N
        processes.sort(key=lambda p: p.memory_mb, reverse=True)
        return processes[:max_results]


# ============================================================================
# FORMATTING & OUTPUT
# ============================================================================

class TooltipFormatter:
    """Handles all tooltip formatting with Pango markup."""
    
    def __init__(self, colors: dict[str, str], color_mgr: ColorManager):
        self._colors = colors
        self._color_mgr = color_mgr
        self._width = Config.TOOLTIP_WIDTH - 2
    
    @staticmethod
    def strip_pango(text: str) -> str:
        """Remove Pango tags for width calculation."""
        text = re.sub(r'<span[^>]*>', '', text)
        text = re.sub(r'</span>', '', text)
        text = re.sub(r'<[^>]+>', '', text)
        return text
    
    def visible_len(self, text: str) -> int:
        return len(self.strip_pango(text))
    
    def center(self, text: str, pad_char: str = ' ') -> str:
        """Center text with Pango support."""
        vlen = self.visible_len(text)
        if vlen >= self._width:
            return text
        left = (self._width - vlen) // 2
        right = self._width - vlen - left
        return f"{pad_char * left}{text}{pad_char * right}"
    
    def left(self, text: str, pad_char: str = ' ') -> str:
        """Left-align text."""
        vlen = self.visible_len(text)
        if vlen >= self._width:
            return text
        return f"{text}{pad_char * (self._width - vlen)}"
    
    def _get_bar_segment(self, val: float, threshold: int) -> str:
        """Generate bar segment with color."""
        char_map = {80: "███", 60: "▅▅▅", 40: "▃▃▃", 20: "▂▂▂", 0: "───"}
        color = (self._color_mgr.get_power_color(val) if val > threshold 
                else self._colors["bright_black"])
        return f"<span foreground='{color}'>{char_map[threshold]}</span>"
    
    def generate_graphic(self, stats: GPUStats) -> list[str]:
        """Generate ASCII graphic representation."""
        temp_color = self._color_mgr.get_temp_color(stats.temperature)
        vram_pct = stats.vram_percent
        
        # VRAM color gradient
        vram_colors = [
            self._color_mgr.get_power_color(vram_pct) if vram_pct > i * (100/6) 
            else self._colors["white"]
            for i in range(6)
        ]
        
        # Build bars
        bars = []
        for thresh in [80, 60, 40, 20, 0]:
            bar_line = (
                f"{self._get_bar_segment(stats.utilization, thresh)} "
                f"{self._get_bar_segment(stats.power_percent, thresh)} "
                f"{self._get_bar_segment(stats.fan_percent, thresh)}"
            )
            bars.append(bar_line)
        
        bg = lambda t: f"<span foreground='{temp_color}'>{t}</span>"
        f_c = self._colors["white"]
        
        return [
            f"<span foreground='{f_c}'>╭─────────────────╮</span>",
            f"<span foreground='{f_c}'> </span><span foreground='{vram_colors[5]}'>███</span><span foreground='{f_c}'> │</span>{bg('░░░░░░░░░░░░░░░░░')}<span foreground='{f_c}'>│ </span><span foreground='{vram_colors[5]}'>███</span><span foreground='{f_c}'> </span>",
            f"<span foreground='{f_c}'> </span><span foreground='{vram_colors[4]}'>███</span><span foreground='{f_c}'> │</span>{bg('░░')}  󰓅      󰈐  {bg('░░')}<span foreground='{f_c}'>│ </span><span foreground='{vram_colors[4]}'>███</span><span foreground='{f_c}'> </span>",
            f"<span foreground='{f_c}'>  │</span>{bg('░░')} {bars[0]} {bg('░░')}<span foreground='{f_c}'>│  </span>",
            f"<span foreground='{f_c}'> </span><span foreground='{vram_colors[3]}'>███</span><span foreground='{f_c}'> │</span>{bg('░░')} {bars[1]} {bg('░░')}<span foreground='{f_c}'>│ </span><span foreground='{vram_colors[3]}'>███</span><span foreground='{f_c}'> </span>",
            f"<span foreground='{f_c}'> </span><span foreground='{vram_colors[2]}'>███</span><span foreground='{f_c}'> │</span>{bg('░░')} {bars[2]} {bg('░░')}<span foreground='{f_c}'>│ </span><span foreground='{vram_colors[2]}'>███</span><span foreground='{f_c}'> </span>",
            f"<span foreground='{f_c}'>  │</span>{bg('░░')} {bars[3]} {bg('░░')}<span foreground='{f_c}'>│  </span>",
            f"<span foreground='{f_c}'> </span><span foreground='{vram_colors[1]}'>███</span><span foreground='{f_c}'> │</span>{bg('░░')} {bars[4]} {bg('░░')}<span foreground='{f_c}'>│ </span><span foreground='{vram_colors[1]}'>███</span><span foreground='{f_c}'> </span>",
            f"<span foreground='{f_c}'> </span><span foreground='{vram_colors[0]}'>███</span><span foreground='{f_c}'> │</span>{bg('░░░░░░░░░░░░░░░░░')}<span foreground='{f_c}'>│ </span><span foreground='{vram_colors[0]}'>███</span><span foreground='{f_c}'> </span>",
            f"<span foreground='{f_c}'>╰─────────────────╯</span>"
        ]
    
    def format_tooltip(self, stats: GPUStats, processes: list[ProcessInfo]) -> str:
        """Generate complete tooltip with all sections."""
        lines = []
        border_color = self._colors["bright_black"]
        separator = "─" * self._width
        
        # Header
        header = self.left(
            f"<span foreground='{self._colors['yellow']}'>{Config.GPU_ICON}</span> "
            f"<span foreground='{self._colors['yellow']}'>GPU</span> - {stats.name}"
        )
        lines.append(self.center(header))
        lines.append(f"<span foreground='{border_color}'>{separator}</span>")
        
        # Stats section
        stats_lines = [
            f" │ Temperature: <span foreground='{self._color_mgr.get_temp_color(stats.temperature)}'>{stats.temperature}°C</span>",
            f"󰘚 │ V-RAM:       <span foreground='{self._color_mgr.get_power_color(stats.vram_percent)}'>{stats.vram_used} / {stats.vram_total} MB</span>",
            f" │ Power:       <span foreground='{self._color_mgr.get_power_color(stats.power_percent)}'>{stats.power_draw:.1f}W / {stats.power_limit:.0f}W</span>",
            f"󰓅 │ Utilization: <span foreground='{self._color_mgr.get_power_color(stats.utilization)}'>{stats.utilization}%</span>",
            f"󰈐 │ Fan Speed:   <span foreground='{self._color_mgr.get_power_color(stats.fan_percent)}'>{stats.fan_rpm} RPM ({stats.fan_percent:.0f}%)</span>"
        ]
        
        for line in stats_lines:
            lines.append(self.left(line))
        
        lines.append("")  # Empty line
        
        # Graphic section
        for line in self.generate_graphic(stats):
            lines.append(self.center(line))
        
        lines.append("")
        
        # Process section
        lines.append(self.left("Top GPU Processes:"))
        
        if processes:
            for proc in processes:
                name = proc.name[:14] + "…" if len(proc.name) > 15 else proc.name
                color = self._color_mgr.get_power_color(proc.memory_mb / 10)
                proc_line = f"• {name:<15} <span foreground='{color}'>󰘚 {proc.memory_mb}MB</span>"
                lines.append(self.left(proc_line))
        else:
            lines.append(self.left("<span size='small'>No active GPU processes detected</span>"))
        
        lines.append("")
        lines.append(f"<span foreground='{border_color}'>{separator}</span>")
        
        # Footer
        lines.append(self.center("󰍽 LMB: CoreCtrl"))
        
        return f"<span size='12000'>{'\n'.join(lines)}</span>"


# ============================================================================
# MAIN MODULE
# ============================================================================

class WaybarGPUModule:
    """Main Waybar GPU module orchestrator."""
    
    def __init__(self):
        self._collector = GPUCollector()
        self._detector = ProcessDetector()
        self._colors = ThemeManager.load()
        self._color_mgr = ColorManager(self._colors)
        self._formatter = TooltipFormatter(self._colors, self._color_mgr)
    
    def run(self) -> None:
        """Execute module and output JSON for Waybar."""
        try:
            # Collect data
            stats = self._collector.collect()
            processes = self._detector.find_gpu_processes()
            
            # Determine output color based on temperature
            temp_color = self._color_mgr.get_temp_color(stats.temperature)
            
            # Build output
            output = {
                "text": f"{Config.GPU_ICON} <span foreground='{temp_color}'>{stats.temperature}°C</span>",
                "tooltip": self._formatter.format_tooltip(stats, processes),
                "markup": "pango",
                "class": "gpu"
            }
            
            print(json.dumps(output))
            
        except Exception as e:
            # Graceful degradation on critical failure
            error_output = {
                "text": f"{Config.GPU_ICON} <span foreground='{self._colors['red']}'>ERR</span>",
                "tooltip": f"<span foreground='{self._colors['red']}'>GPU module error: {str(e)}</span>",
                "markup": "pango",
                "class": "gpu error"
            }
            print(json.dumps(error_output))
            sys.exit(1)


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    module = WaybarGPUModule()
    module.run()
