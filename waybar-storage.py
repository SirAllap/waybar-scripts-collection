#!/usr/bin/env python3
"""
Waybar Storage Module - Optimized System Monitor
Monitors physical drive usage, I/O speeds, temperature, and health status.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Optional

import psutil

# ============================================================================
# CONFIGURATION
# ============================================================================

def _parse_drive_names() -> dict[str, str]:
    """Load drive labels from WAYBAR_STORAGE_NAMES env var.

    Format: "device=Label,device=Label"
    Example: WAYBAR_STORAGE_NAMES="nvme0n1=System,sda=Storage,nvme1n1=Games"
    Falls back to generic names when the variable is not set.
    """
    env = os.environ.get("WAYBAR_STORAGE_NAMES", "").strip()
    if env:
        names: dict[str, str] = {}
        for entry in env.split(","):
            if "=" in entry:
                dev, _, label = entry.partition("=")
                names[dev.strip()] = label.strip()
        if names:
            return names
    return {
        "nvme0n1": "System",
        "nvme1n1": "Secondary",
        "sda":     "Storage",
    }


@dataclass(frozen=True)
class Config:
    """Immutable configuration constants."""
    HISTORY_FILE: Path = Path("/tmp/waybar_storage_history.json")
    UPDATE_INTERVAL: float = 2.0  # Minimum seconds between I/O calculations
    TEMP_CACHE_TTL: float = 30.0  # Seconds to cache temperature/SMART data
    TOOLTIP_WIDTH: int = 45
    TIMEOUT_SMART: int = 3
    TIMEOUT_SENSORS: int = 2
    
    # Icons
    SSD_ICON: str = ""
    HDD_ICON: str = "󰋊"
    
    # Drive name mapping — loaded from WAYBAR_STORAGE_NAMES env var.
    # Format: "device=Label,device=Label"  (comma-separated, one = per entry)
    # Example: WAYBAR_STORAGE_NAMES="nvme0n1=System,sda=Storage,nvme1n1=Games"
    # Falls back to generic names if the variable is not set.
    # Run `lsblk -d -o NAME` to list your device names.
    DRIVE_NAMES: dict[str, str] = field(default_factory=lambda: _parse_drive_names())

CONFIG: Final = Config()

# ============================================================================
# COLOR MANAGEMENT
# ============================================================================

@dataclass(frozen=True)
class ColorTheme:
    """Immutable color theme loaded once."""
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
    def load(cls) -> "ColorTheme":
        """Load theme from Omarchy config or return defaults."""
        theme_path = Path.home() / ".config/omarchy/current/theme/colors.toml"
        
        if not theme_path.exists():
            return cls()
        
        try:
            import tomllib
            data = tomllib.loads(theme_path.read_text())
            return cls(
                black=data.get("color0", "#000000"),
                red=data.get("color1", "#ff0000"),
                green=data.get("color2", "#00ff00"),
                yellow=data.get("color3", "#ffff00"),
                blue=data.get("color4", "#0000ff"),
                magenta=data.get("color5", "#ff00ff"),
                cyan=data.get("color6", "#00ffff"),
                white=data.get("color7", "#ffffff"),
                bright_black=data.get("color8", "#555555"),
                bright_red=data.get("color9", "#ff5555"),
                bright_green=data.get("color10", "#55ff55"),
                bright_yellow=data.get("color11", "#ffff55"),
                bright_blue=data.get("color12", "#5555ff"),
                bright_magenta=data.get("color13", "#ff55ff"),
                bright_cyan=data.get("color14", "#55ffff"),
                bright_white=data.get("color15", "#ffffff"),
            )
        except Exception:
            return cls()


# Load once at module level
COLORS: Final = ColorTheme.load()


class ColorScale:
    """Color interpolation for metrics."""
    
    # (threshold%, color) tuples - must be sorted by threshold
    USAGE_SCALE: Final[list[tuple[float, str]]] = [
        (0.0, COLORS.blue),
        (10.0, COLORS.cyan),
        (20.0, COLORS.green),
        (40.0, COLORS.yellow),
        (60.0, COLORS.bright_yellow),
        (80.0, COLORS.bright_red),
        (90.0, COLORS.red),
    ]
    
    # (threshold_temp, color) tuples
    TEMP_SCALE: Final[list[tuple[int, str]]] = [
        (0, COLORS.blue),
        (36, COLORS.cyan),
        (46, COLORS.green),
        (55, COLORS.yellow),
        (61, COLORS.bright_yellow),
        (71, COLORS.bright_red),
        (81, COLORS.red),
    ]
    
    @classmethod
    def get(cls, value: float | int | None, scale: list[tuple[float | int, str]]) -> str:
        """Get color for value based on scale."""
        if value is None:
            return COLORS.white
        
        try:
            val = float(value)
        except (TypeError, ValueError):
            return COLORS.white
        
        # Find appropriate color
        result = scale[0][1]
        for threshold, color in scale:
            if val >= threshold:
                result = color
            else:
                break
        return result


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class DriveInfo:
    """Represents a physical storage drive."""
    name: str
    mountpoint: str
    device: str  # Base device name (e.g., nvme0n1, sda)
    is_hdd: bool
    total_bytes: int
    used_percent: int
    
    # Optional metrics
    temperature: Optional[int] = None
    health: Optional[str] = None
    lifespan: Optional[str] = None
    tbw: Optional[str] = None
    read_speed: float = 0.0
    write_speed: float = 0.0
    
    @property
    def icon(self) -> str:
        return CONFIG.HDD_ICON if self.is_hdd else CONFIG.SSD_ICON
    
    @property
    def total_tb(self) -> float:
        return self.total_bytes / (1024 ** 4)


@dataclass
class IOHistory:
    """I/O counter history for speed calculation."""
    read_bytes: int
    write_bytes: int
    timestamp: float
    
    def calculate_speed(self, current: "IOHistory", device: str) -> tuple[float, float]:
        """Calculate read/write speeds with validation."""
        dt = current.timestamp - self.timestamp
        
        # Sanity checks
        if dt < CONFIG.UPDATE_INTERVAL or dt > 300:  # Max 5 minutes
            return 0.0, 0.0
        
        # Handle counter wraparound (rare but possible)
        r_bytes = current.read_bytes - self.read_bytes
        w_bytes = current.write_bytes - self.write_bytes
        
        if r_bytes < 0 or w_bytes < 0:
            return 0.0, 0.0
        
        return r_bytes / dt, w_bytes / dt


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def format_bytes_compact(bytes_val: float, suffix: str = "/s") -> str:
    """Format bytes to human readable with appropriate unit."""
    if bytes_val < 0 or not bytes_val:
        return f"0{suffix}"
    
    units = [("B", 1), ("K", 1024), ("M", 1024**2), ("G", 1024**3), ("T", 1024**4)]
    
    for unit, divisor in reversed(units):
        if bytes_val >= divisor:
            val = bytes_val / divisor
            if val >= 100:
                return f"{val:.0f}{unit}{suffix}"
            return f"{val:.1f}{unit}{suffix}"
    
    return f"{bytes_val:.0f}B{suffix}"


def normalize_device_name(device: str) -> str:
    """Remove partition numbers to get base device name."""
    if device.startswith("nvme"):
        return re.sub(r"p\d+$", "", device)
    return re.sub(r"\d+$", "", device)


@lru_cache(maxsize=32)
def resolve_physical_device(device_path: str) -> str:
    """
    Resolve device path to physical base device with caching.
    Handles LVM, cryptsetup, and device-mapper.
    """
    try:
        # Resolve symlinks
        real_path = os.path.realpath(device_path)
        name = os.path.basename(real_path)
        
        # Handle device-mapper
        if name.startswith("dm-"):
            slaves_path = Path(f"/sys/class/block/{name}/slaves")
            if slaves_path.exists():
                slaves = list(slaves_path.iterdir())
                if slaves:
                    name = slaves[0].name
        
        return normalize_device_name(name)
    except (OSError, ValueError):
        return normalize_device_name(os.path.basename(device_path))


def is_rotational_disk(device: str) -> bool:
    """Check if device is HDD (rotational) or SSD/NVMe."""
    try:
        rotational_file = Path(f"/sys/class/block/{device}/queue/rotational")
        if rotational_file.exists():
            return rotational_file.read_text().strip() == "1"
    except (OSError, IOError):
        pass
    return False  # Default to SSD for safety


# ============================================================================
# HARDWARE MONITORING (with caching)
# ============================================================================

class HardwareMonitor:
    """Cached hardware sensor monitoring."""
    
    def __init__(self):
        self._nvme_pci_map: Optional[dict[str, str]] = None
        self._sensors_data: Optional[dict] = None
        self._sensors_timestamp: float = 0.0
        self._smart_cache: dict[str, tuple[dict, float]] = {}
    
    def _get_nvme_pci_mapping(self) -> dict[str, str]:
        """Build NVMe device to PCI address mapping."""
        if self._nvme_pci_map is not None:
            return self._nvme_pci_map
        
        mapping = {}
        nvme_path = Path("/sys/class/nvme")
        
        if not nvme_path.exists():
            self._nvme_pci_map = mapping
            return mapping
        
        for dev_dir in nvme_path.iterdir():
            try:
                device_link = dev_dir / "device"
                if device_link.is_symlink():
                    pci_addr = os.readlink(device_link).split("/")[-1]
                    parts = pci_addr.split(":")
                    if len(parts) == 3:
                        bus = parts[1].lstrip("0") or "0"
                        device_fn = parts[2].replace(".", "")
                        mapping[dev_dir.name] = f"{bus}{device_fn}"
            except (OSError, ValueError):
                continue
        
        self._nvme_pci_map = mapping
        return mapping
    
    def _get_sensors_data(self) -> Optional[dict]:
        """Get sensors data with caching."""
        now = time.time()
        
        if (self._sensors_data is not None and 
            now - self._sensors_timestamp < CONFIG.TEMP_CACHE_TTL):
            return self._sensors_data
        
        try:
            result = subprocess.run(
                ["sensors", "-j"],
                capture_output=True,
                text=True,
                timeout=CONFIG.TIMEOUT_SENSORS
            )
            if result.returncode == 0:
                self._sensors_data = json.loads(result.stdout)
                self._sensors_timestamp = now
                return self._sensors_data
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass
        
        return None
    
    def get_temperature(self, device: str) -> Optional[int]:
        """Get drive temperature from sensors or smartctl."""
        # Try sensors first (faster, no sudo)
        temp = self._get_temp_from_sensors(device)
        if temp is not None:
            return temp
        
        # Fallback to smartctl
        return self._get_temp_from_smartctl(device)
    
    def _get_temp_from_sensors(self, device: str) -> Optional[int]:
        """Extract temperature from lm_sensors data."""
        data = self._get_sensors_data()
        if not data:
            return None
        
        try:
            if device.startswith("nvme"):
                pci_map = self._get_nvme_pci_mapping()
                pci_addr = pci_map.get(device)
                if pci_addr:
                    sensor_key = f"nvme-pci-{pci_addr}"
                    if sensor_key in data:
                        # Look for Composite temperature
                        for sub_val in data[sensor_key].values():
                            if isinstance(sub_val, dict):
                                if "temp1_input" in sub_val:
                                    return int(sub_val["temp1_input"])
                
                # Fallback: try any nvme-pci sensor
                for key, val in data.items():
                    if key.startswith("nvme-pci-"):
                        for sub_val in val.values():
                            if isinstance(sub_val, dict) and "temp1_input" in sub_val:
                                return int(sub_val["temp1_input"])
            else:
                # SATA drives
                for key, val in data.items():
                    if device in key.lower():
                        for sub_val in val.values():
                            if isinstance(sub_val, dict) and "temp1_input" in sub_val:
                                return int(sub_val["temp1_input"])
        except (KeyError, ValueError, TypeError):
            pass
        
        return None
    
    def _get_temp_from_smartctl(self, device: str) -> Optional[int]:
        """Get temperature via smartctl (requires sudo)."""
        cache_key = f"temp_{device}"
        now = time.time()
        
        # Check cache
        if cache_key in self._smart_cache:
            data, timestamp = self._smart_cache[cache_key]
            if now - timestamp < CONFIG.TEMP_CACHE_TTL:
                return data.get("temperature", {}).get("current")
        
        try:
            result = subprocess.run(
                ["sudo", "-n", "smartctl", "-A", f"/dev/{device}", "-j"],
                capture_output=True,
                text=True,
                timeout=CONFIG.TIMEOUT_SMART
            )
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                self._smart_cache[cache_key] = (data, now)
                return data.get("temperature", {}).get("current")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass
        
        return None
    
    def get_smart_info(self, device: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Get SMART health, lifespan, and TBW."""
        cache_key = f"smart_{device}"
        now = time.time()
        
        # Check cache
        if cache_key in self._smart_cache:
            data, timestamp = self._smart_cache[cache_key]
            if now - timestamp < CONFIG.TEMP_CACHE_TTL:
                return self._parse_smart_data(data)
        
        try:
            result = subprocess.run(
                ["sudo", "-n", "smartctl", "-a", "-j", f"/dev/{device}"],
                capture_output=True,
                text=True,
                timeout=CONFIG.TIMEOUT_SMART
            )
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                self._smart_cache[cache_key] = (data, now)
                return self._parse_smart_data(data)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass
        
        return None, None, None
    
    def _parse_smart_data(self, data: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Parse SMART JSON data."""
        smart_status = data.get("smart_status", {})
        passed = smart_status.get("passed")
        health = "OK" if passed else "FAIL" if passed is False else None
        
        lifespan = None
        tbw = None
        
        # NVMe specific
        nvme_log = data.get("nvme_smart_health_information_log", {})
        if nvme_log:
            used = nvme_log.get("percentage_used")
            if used is not None:
                lifespan = f"{max(0, 100 - used)}%"
            
            duw = nvme_log.get("data_units_written")
            if duw:
                tbw = f"{(duw * 512000) / 1e12:.1f} TB"
        else:
            # ATA/SATA
            attrs = {
                a.get("id"): a 
                for a in data.get("ata_smart_attributes", {}).get("table", [])
            }
            
            # Estimate lifespan from power-on hours (id 9)
            if 9 in attrs:
                poh = attrs[9].get("raw", {}).get("value", 0)
                if poh > 0:
                    lifespan_est = max(0, 100 - (poh / 43800 * 100))
                    lifespan = f"~{lifespan_est:.0f}%"
            
            # TBW from total LBAs written (id 241)
            if 241 in attrs:
                lba_written = attrs[241].get("raw", {}).get("value", 0)
                if lba_written:
                    tbw_calc = (lba_written * 512) / (1024 ** 4)
                    tbw = f"~{tbw_calc:.1f} TB"
        
        return health, lifespan, tbw


# ============================================================================
# DRIVE DETECTION
# ============================================================================

class DriveDetector:
    """Detects and filters physical drives."""
    
    # Filesystems to monitor
    VALID_FSTYPES: Final[set[str]] = {
        "ext4", "btrfs", "xfs", "ntfs", "vfat", "apfs", 
        "zfs", "exfat", "crypto_LUKS", "f2fs"
    }
    
    # Paths to exclude
    EXCLUDE_PATTERNS: Final[set[str]] = {
        "/snap", "/boot", "/docker", "/run", "/sys", 
        "/proc", "/dev", "/tmp"
    }
    
    def __init__(self, monitor: HardwareMonitor):
        self.monitor = monitor
    
    def get_drives(self) -> list[DriveInfo]:
        """Get list of physical drives with metrics."""
        drives = []
        seen_devices: set[str] = set()
        
        try:
            partitions = psutil.disk_partitions(all=False)
        except Exception:
            return drives
        
        for part in partitions:
            # Filter by mountpoint
            if any(excl in part.mountpoint for excl in self.EXCLUDE_PATTERNS):
                continue
            
            # Filter by filesystem
            if not part.fstype or part.fstype not in self.VALID_FSTYPES:
                continue
            
            # Get physical device
            physical_dev = resolve_physical_device(part.device)
            
            # Deduplicate
            if physical_dev in seen_devices:
                continue
            seen_devices.add(physical_dev)
            
            # Get usage stats
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (OSError, PermissionError):
                continue
            
            # Determine name
            name = self._get_drive_name(physical_dev, part.mountpoint)
            
            # Detect type
            is_hdd = is_rotational_disk(physical_dev)
            
            # Get hardware info
            temp = self.monitor.get_temperature(physical_dev)
            health, lifespan, tbw = self.monitor.get_smart_info(physical_dev)
            
            drives.append(DriveInfo(
                name=name,
                mountpoint=part.mountpoint,
                device=physical_dev,
                is_hdd=is_hdd,
                total_bytes=usage.total,
                used_percent=int(usage.percent),
                temperature=temp,
                health=health,
                lifespan=lifespan,
                tbw=tbw
            ))
        
        return drives
    
    def _get_drive_name(self, device: str, mountpoint: str) -> str:
        """Determine display name for drive."""
        if device in CONFIG.DRIVE_NAMES:
            return CONFIG.DRIVE_NAMES[device]
        
        if mountpoint == "/":
            return "Root"
        if mountpoint == "/home":
            return "Home"
        if mountpoint.startswith("/mnt/"):
            return os.path.basename(mountpoint)
        
        return os.path.basename(mountpoint) or "Unknown"


# ============================================================================
# I/O MONITORING
# ============================================================================

class IOMonitor:
    """Manages I/O counter history and speed calculation."""
    
    def __init__(self):
        self.history: dict[str, IOHistory] = {}
        self._load_history()
    
    def _load_history(self) -> None:
        """Load previous I/O counters from file."""
        try:
            if CONFIG.HISTORY_FILE.exists():
                data = json.loads(CONFIG.HISTORY_FILE.read_text())
                self.history = {
                    k: IOHistory(
                        read_bytes=v["r"],
                        write_bytes=v["w"],
                        timestamp=v["t"]
                    )
                    for k, v in data.get("io", {}).items()
                }
        except (json.JSONDecodeError, KeyError, IOError):
            self.history = {}
    
    def save_history(self, current: dict[str, IOHistory]) -> None:
        """Save current I/O counters to file."""
        try:
            data = {
                "io": {
                    k: {"r": v.read_bytes, "w": v.write_bytes, "t": v.timestamp}
                    for k, v in current.items()
                }
            }
            CONFIG.HISTORY_FILE.write_text(json.dumps(data))
        except IOError:
            pass
    
    def get_io_counters(self) -> dict[str, psutil._psplatform.DiskIOCounters]:
        """Get current I/O counters per disk."""
        try:
            return psutil.disk_io_counters(perdisk=True) or {}
        except Exception:
            return {}
    
    def calculate_speeds(self, drives: list[DriveInfo]) -> None:
        """Calculate I/O speeds for drives."""
        current_io = self.get_io_counters()
        current_time = time.time()
        new_history: dict[str, IOHistory] = {}
        
        for drive in drives:
            if drive.device not in current_io:
                continue
            
            counters = current_io[drive.device]
            new_history[drive.device] = IOHistory(
                read_bytes=counters.read_bytes,
                write_bytes=counters.write_bytes,
                timestamp=current_time
            )
            
            # Calculate speed if we have history
            if drive.device in self.history:
                old = self.history[drive.device]
                drive.read_speed, drive.write_speed = old.calculate_speed(
                    new_history[drive.device], drive.device
                )
        
        self.save_history(new_history)


# ============================================================================
# OUTPUT FORMATTING
# ============================================================================

class TooltipFormatter:
    """Formats drive data into Waybar tooltip."""
    
    def __init__(self):
        self.lines: list[str] = []
    
    def format_drive(self, drive: DriveInfo) -> None:
        """Format single drive entry."""
        # Color selection
        temp_color = ColorScale.get(drive.temperature, ColorScale.TEMP_SCALE)
        usage_color = ColorScale.get(drive.used_percent, ColorScale.USAGE_SCALE)
        
        # Header
        size_str = f"{drive.total_tb:.1f}TB"
        self.lines.append(
            f"{drive.icon} <span foreground='{COLORS.white}'><b>{drive.name}</b></span> - {size_str}"
        )
        
        # Temperature
        temp_str = f"{drive.temperature}°C" if drive.temperature else "N/A"
        self.lines.append(f"<span foreground='{temp_color}'></span> │ <span foreground='{temp_color}'>{temp_str}</span>")
        
        # Lifespan/TBW
        if drive.lifespan:
            self.lines.append(
                f"<span foreground='{COLORS.yellow}'></span> │ "
                f"<span foreground='{COLORS.white}'>Lifespan: {drive.lifespan}</span>"
            )
        elif drive.tbw:
            self.lines.append(
                f"<span foreground='{COLORS.yellow}'></span> │ "
                f"<span foreground='{COLORS.white}'>TB Written: {drive.tbw}</span>"
            )
        
        # Health
        if drive.health:
            health_color = COLORS.green if drive.health == "OK" else COLORS.red
            health_icon = "" if drive.health == "OK" else ""
            self.lines.append(
                f"<span foreground='{health_color}'>{health_icon}</span> │ "
                f"<span foreground='{COLORS.white}'>Health: </span>"
                f"<span foreground='{health_color}'>{drive.health}</span>"
            )
        
        # I/O Speeds
        rs = format_bytes_compact(drive.read_speed)
        ws = format_bytes_compact(drive.write_speed)
        self.lines.append(
            f"<span size='small'>"
            f"<span foreground='{COLORS.green}'></span> Write: <span foreground='{COLORS.green}'>{ws}</span>  "
            f"<span foreground='{COLORS.blue}'></span> Read: <span foreground='{COLORS.blue}'>{rs}</span>"
            f"</span>"
        )
        
        # Progress bar
        bar = self._create_progress_bar(drive.used_percent, usage_color)
        self.lines.append(f"{CONFIG.SSD_ICON} {bar} <span foreground='{usage_color}'>{drive.used_percent}%</span>")
        self.lines.append("")
    
    def _create_progress_bar(self, percent: int, color: str, width: int = 25) -> str:
        """Create ASCII progress bar."""
        filled = int((percent / 100) * width)
        filled_chars = "█" * filled
        empty_chars = "░" * (width - filled)
        return f"<span foreground='{color}'>{filled_chars}</span><span foreground='{COLORS.bright_black}'>{empty_chars}</span>"
    
    def get_tooltip(self, drives: list[DriveInfo]) -> str:
        """Generate complete tooltip."""
        self.lines = [
            f"<span foreground='{COLORS.blue}'>{CONFIG.SSD_ICON}</span> <span foreground='{COLORS.white}'>Storage</span>",
            f"<span foreground='{COLORS.bright_black}'>{'─' * CONFIG.TOOLTIP_WIDTH}</span>"
        ]
        
        for drive in drives:
            self.format_drive(drive)

        self.lines.append(f"<span foreground='{COLORS.bright_black}'>{'─' * CONFIG.TOOLTIP_WIDTH}</span>")
        self.lines.append("󰍽 LMB: Disk Usage")

        return f"<span size='12000'>{'\n'.join(self.lines)}</span>"


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:
    """Main entry point."""
    try:
        # Initialize components
        monitor = HardwareMonitor()
        detector = DriveDetector(monitor)
        io_monitor = IOMonitor()
        formatter = TooltipFormatter()
        
        # Get drive data
        drives = detector.get_drives()
        
        # Calculate I/O speeds
        io_monitor.calculate_speeds(drives)
        
        # Find root usage for main text
        root_usage = next(
            (d.used_percent for d in drives if d.mountpoint == "/"),
            0
        )
        
        # Generate output
        tooltip = formatter.get_tooltip(drives)
        usage_color = ColorScale.get(root_usage, ColorScale.USAGE_SCALE)
        
        output = {
            "text": f"{CONFIG.SSD_ICON} <span foreground='{usage_color}'>{root_usage}%</span>",
            "tooltip": tooltip,
            "markup": "pango",
            "class": "storage"
        }
        
        print(json.dumps(output))
        
    except Exception as e:
        # Graceful failure
        print(json.dumps({
            "text": f"{CONFIG.SSD_ICON} --",
            "tooltip": f"<span foreground='{COLORS.red}'>Error: {str(e)}</span>",
            "markup": "pango",
            "class": "storage error"
        }))


if __name__ == "__main__":
    main()
