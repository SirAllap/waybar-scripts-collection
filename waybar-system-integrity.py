#!/usr/bin/env python3
"""
Waybar System Integrity Module

A high-performance, modular system health monitor for Waybar.
Features caching, async execution, and robust error handling.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Protocol, Tuple, Union

import psutil

# Optional imports with graceful degradation
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

# Configure logging
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("waybar-integrity")


class Status(Enum):
    """Health status levels."""
    OK = ("OK", "\uf058", "green")
    WARNING = ("WARNING", "\uf071", "yellow")
    CRITICAL = ("CRITICAL", "\uf057", "red")
    UNKNOWN = ("UNKNOWN", "\uf128", "bright_black")

    def __init__(self, label: str, icon: str, color_key: str):
        self.label = label
        self.icon = icon
        self.color_key = color_key


@dataclass(frozen=True)
class CheckResult:
    """Immutable result from a system check."""
    status: Status
    message: Optional[str] = None
    details: Tuple[str, ...] = field(default_factory=tuple)
    metrics: Dict[str, Union[int, float, str]] = field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        return self.status == Status.OK


class ThemeColors:
    """Lazy-loaded theme colors with fallback defaults."""
    
    DEFAULTS: ClassVar[Dict[str, str]] = {
        "black": "#000000",
        "red": "#ff0000",
        "green": "#00ff00",
        "yellow": "#ffff00",
        "blue": "#0000ff",
        "magenta": "#ff00ff",
        "cyan": "#00ffff",
        "white": "#ffffff",
        "bright_black": "#555555",
        "bright_red": "#ff5555",
        "bright_green": "#55ff55",
        "bright_yellow": "#ffff55",
        "bright_blue": "#5555ff",
        "bright_magenta": "#ff55ff",
        "bright_cyan": "#55ffff",
        "bright_white": "#ffffff",
    }

    def __init__(self):
        self._colors: Optional[Dict[str, str]] = None

    def _load(self) -> Dict[str, str]:
        """Load colors from theme file."""
        if not tomllib:
            return self.DEFAULTS.copy()

        theme_path = Path.home() / ".config/omarchy/current/theme/colors.toml"
        if not theme_path.exists():
            return self.DEFAULTS.copy()

        try:
            data = tomllib.loads(theme_path.read_text(encoding="utf-8"))
            colors = {
                f"color{i}": data.get(f"color{i}", self.DEFAULTS["black"])
                for i in range(16)
            }
            return {
                "black": colors["color0"],
                "red": colors["color1"],
                "green": colors["color2"],
                "yellow": colors["color3"],
                "blue": colors["color4"],
                "magenta": colors["color5"],
                "cyan": colors["color6"],
                "white": colors["color7"],
                "bright_black": colors["color8"],
                "bright_red": colors["color9"],
                "bright_green": colors["color10"],
                "bright_yellow": colors["color11"],
                "bright_blue": colors["color12"],
                "bright_magenta": colors["color13"],
                "bright_cyan": colors["color14"],
                "bright_white": colors["color15"],
            }
        except Exception as e:
            logger.warning(f"Failed to load theme: {e}")
            return self.DEFAULTS.copy()

    def __getitem__(self, key: str) -> str:
        if self._colors is None:
            self._colors = self._load()
        return self._colors.get(key, self.DEFAULTS.get(key, "#ffffff"))


# Global theme instance
COLORS = ThemeColors()


class SystemCheck(ABC):
    """Abstract base class for system checks."""
    
    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout
        self._cache: Optional[Tuple[datetime, CheckResult]] = None
        self._cache_ttl = timedelta(seconds=10)

    @property
    @abstractmethod
    def name(self) -> str:
        """Check identifier."""
        pass

    async def run(self, use_cache: bool = True) -> CheckResult:
        """Execute check with optional caching."""
        if use_cache and self._cache:
            timestamp, result = self._cache
            if datetime.now() - timestamp < self._cache_ttl:
                return result

        try:
            result = await self._execute()
        except Exception as e:
            logger.error(f"Check {self.name} failed: {e}")
            result = CheckResult(
                status=Status.UNKNOWN,
                message=f"Check failed: {type(e).__name__}"
            )

        self._cache = (datetime.now(), result)
        return result

    @abstractmethod
    async def _execute(self) -> CheckResult:
        """Implement actual check logic."""
        pass

    async def _run_cmd(
        self,
        cmd: List[str],
        shell: bool = False,
        check: bool = False
    ) -> Tuple[int, str, str]:
        """Execute command asynchronously with timeout."""
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ) if not shell else asyncio.create_subprocess_shell(
                    cmd[0],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=self.timeout
            )
            stdout, stderr = await proc.communicate()
            return (
                proc.returncode or 0,
                stdout.decode("utf-8", errors="replace").strip(),
                stderr.decode("utf-8", errors="replace").strip()
            )
        except asyncio.TimeoutError:
            logger.warning(f"Command timed out: {cmd}")
            return -1, "", "Timeout"
        except Exception as e:
            logger.error(f"Command failed: {cmd} - {e}")
            return -1, "", str(e)


class SystemdCheck(SystemCheck):
    """Check systemd service status."""
    
    @property
    def name(self) -> str:
        return "Systemd Services"

    async def _execute(self) -> CheckResult:
        code, stdout, _ = await self._run_cmd([
            "systemctl", "--failed", "--no-legend", "--quiet"
        ])
        
        if code != 0:
            return CheckResult(Status.UNKNOWN, "Cannot query systemd")
        
        if not stdout:
            return CheckResult(Status.OK, "All services healthy")
        
        failed = [
            line.split()[1] for line in stdout.splitlines()
            if line.strip() and len(line.split()) > 1
        ]
        return CheckResult(
            status=Status.WARNING if len(failed) < 3 else Status.CRITICAL,
            message=f"{len(failed)} failed service(s)",
            details=tuple(failed[:5])
        )


class DiskHealthCheck(SystemCheck):
    """Check disk SMART status."""
    
    def __init__(self):
        super().__init__(timeout=10.0)
        self._smartctl_available = shutil.which("smartctl") is not None

    @property
    def name(self) -> str:
        return "Disk Health"

    async def _execute(self) -> CheckResult:
        if not self._smartctl_available:
            return CheckResult(Status.UNKNOWN, "smartctl not installed")

        # Get block devices
        code, stdout, _ = await self._run_cmd([
            "lsblk", "-d", "-n", "-o", "NAME,TYPE"
        ])
        
        if code != 0:
            return CheckResult(Status.UNKNOWN, "Cannot list block devices")

        devices = [
            line.split()[0] for line in stdout.splitlines()
            if line.strip() and not line.startswith("loop")
        ]

        issues = []
        for dev in devices[:4]:  # Limit parallel checks
            # Skip non-disk devices
            if any(x in dev for x in ("rom", "usb", "virt")):
                continue
                
            code, stdout, _ = await self._run_cmd([
                "sudo", "-n", "smartctl", "-H", f"/dev/{dev}"
            ])
            
            if code != 0:
                continue  # No sudo access or not a SMART device
            
            if stdout and not any(x in stdout for x in ("PASSED", "OK")):
                issues.append(f"{dev}: SMART warning")

        if issues:
            return CheckResult(
                Status.WARNING,
                f"{len(issues)} disk(s) with warnings",
                details=tuple(issues)
            )
        return CheckResult(Status.OK, "All disks healthy")


class UpdatesCheck(SystemCheck):
    """Check for system updates."""
    
    def __init__(self):
        super().__init__()
        self._checkupdates_available = shutil.which("checkupdates") is not None

    @property
    def name(self) -> str:
        return "System Updates"

    async def _execute(self) -> CheckResult:
        if not self._checkupdates_available:
            return CheckResult(Status.UNKNOWN, "checkupdates not available")

        code, stdout, _ = await self._run_cmd(["checkupdates"])
        
        # checkupdates returns 2 when no updates, 0 when updates available
        if code == 2 or not stdout:
            return CheckResult(Status.OK, "System up to date", metrics={"count": 0})
        
        count = len([l for l in stdout.splitlines() if l.strip()])
        
        status = Status.OK if count < 20 else Status.WARNING if count < 50 else Status.CRITICAL
        return CheckResult(
            status,
            f"{count} update(s) available",
            metrics={"count": count}
        )


class SecurityCheck(SystemCheck):
    """Check security status."""
    
    @property
    def name(self) -> str:
        return "Security"

    async def _execute(self) -> CheckResult:
        issues = []
        
        # Check firewall
        for service in ["firewalld", "ufw", "iptables"]:
            code, _, _ = await self._run_cmd(["systemctl", "is-active", service])
            if code == 0:
                break
        else:
            issues.append("No active firewall")
        
        # Check SSH on default port
        code, _, _ = await self._run_cmd(["systemctl", "is-active", "sshd"])
        if code == 0:
            # Check if listening on 22
            code, stdout, _ = await self._run_cmd([
                "ss", "-tlnp", "sport", "=", ":22"
            ])
            if code == 0 and "sshd" in stdout:
                issues.append("SSH on default port (22)")
        
        # Check recent failed logins via journald (more reliable than auth.log)
        code, stdout, _ = await self._run_cmd([
            "journalctl", "-u", "sshd", "--since", "1 hour ago",
            "-g", "Failed password", "--output=cat", "-q"
        ])
        if code == 0:
            failed_count = len(stdout.splitlines())
            if failed_count > 5:
                issues.append(f"{failed_count} failed SSH attempts (1h)")

        if issues:
            return CheckResult(Status.WARNING, "Security concerns detected", details=tuple(issues))
        return CheckResult(Status.OK, "Security checks passed")


class SystemErrorsCheck(SystemCheck):
    """Check for system errors."""
    
    @property
    def name(self) -> str:
        return "System Errors"

    async def _execute(self) -> CheckResult:
        errors = []
        
        # Check dmesg for errors (requires specific groups or sudo usually)
        code, stdout, _ = await self._run_cmd(["dmesg", "-l", "err,crit,alert,emerg"])
        if code == 0 and stdout:
            err_count = len([l for l in stdout.splitlines() if l.strip()])
            if err_count > 0:
                errors.append(f"{err_count} kernel error(s)")
        
        # Check journal for service failures
        code, stdout, _ = await self._run_cmd([
            "journalctl", "-p", "err", "--since", "1 hour ago", "--no-legend", "-q"
        ])
        if code == 0:
            err_count = len([l for l in stdout.splitlines() if l.strip()])
            if err_count > 10:
                errors.append(f"{err_count} error(s) in journal (1h)")

        if errors:
            return CheckResult(Status.WARNING, "Errors detected", details=tuple(errors))
        return CheckResult(Status.OK, "No critical errors")


class DiskSpaceCheck(SystemCheck):
    """Check disk space usage."""
    
    THRESHOLDS = [(90, Status.CRITICAL), (80, Status.WARNING)]

    @property
    def name(self) -> str:
        return "Disk Space"

    async def _execute(self) -> CheckResult:
        warnings = []
        
        # Use psutil for efficiency
        for part in psutil.disk_partitions(all=False):
            if not part.fstype or part.fstype in ("squashfs", "tmpfs"):
                continue
            
            try:
                usage = psutil.disk_usage(part.mountpoint)
                for threshold, status in self.THRESHOLDS:
                    if usage.percent >= threshold:
                        warnings.append(
                            f"{part.mountpoint}: {usage.percent:.1f}% ({status.label})"
                        )
                        break
            except (PermissionError, OSError) as e:
                logger.debug(f"Cannot check {part.mountpoint}: {e}")

        if warnings:
            return CheckResult(
                Status.WARNING if any("CRITICAL" not in w for w in warnings) else Status.CRITICAL,
                f"{len(warnings)} partition(s) full",
                details=tuple(warnings)
            )
        return CheckResult(Status.OK, "Disk space OK")


class MemoryCheck(SystemCheck):
    """Check memory pressure."""
    
    THRESHOLDS = [(95, Status.CRITICAL), (85, Status.WARNING)]

    @property
    def name(self) -> str:
        return "Memory"

    async def _execute(self) -> CheckResult:
        mem = psutil.virtual_memory()
        
        for threshold, status in self.THRESHOLDS:
            if mem.percent >= threshold:
                return CheckResult(
                    status,
                    f"Memory usage: {mem.percent}%",
                    metrics={"percent": mem.percent, "available_gb": mem.available / 1e9}
                )
        
        return CheckResult(
            Status.OK,
            f"Memory OK ({mem.percent}%)",
            metrics={"percent": mem.percent}
        )


class CpuCheck(SystemCheck):
    """Check CPU load."""
    
    @property
    def name(self) -> str:
        return "CPU Load"

    async def _execute(self) -> CheckResult:
        try:
            load1, _, _ = os.getloadavg()
            cpu_count = psutil.cpu_count() or 1
            load_percent = (load1 / cpu_count) * 100
            
            if load1 > cpu_count * 2:
                return CheckResult(
                    Status.WARNING,
                    f"High load: {load1:.2f} ({load_percent:.0f}%)",
                    metrics={"load": load1, "cores": cpu_count}
                )
            return CheckResult(
                Status.OK,
                f"Load: {load1:.2f}",
                metrics={"load": load1}
            )
        except OSError as e:
            return CheckResult(Status.UNKNOWN, f"Cannot read load: {e}")


class TemperatureCheck(SystemCheck):
    """Check system temperatures."""
    
    THRESHOLDS = [(85, Status.CRITICAL), (75, Status.WARNING)]

    @property
    def name(self) -> str:
        return "Temperatures"

    async def _execute(self) -> CheckResult:
        try:
            temps = psutil.sensors_temperatures()
            if not temps:
                return CheckResult(Status.UNKNOWN, "No temperature sensors")
            
            warnings = []
            max_temp = 0.0
            
            for name, entries in temps.items():
                for entry in entries:
                    if entry.current is None:
                        continue
                    max_temp = max(max_temp, entry.current)
                    
                    for threshold, status in self.THRESHOLDS:
                        if entry.current >= threshold:
                            warnings.append(f"{name}: {entry.current:.1f}°C")
                            break
            
            if warnings:
                status = Status.CRITICAL if max_temp >= 85 else Status.WARNING
                return CheckResult(status, f"High temp: {max_temp:.1f}°C", details=tuple(warnings))
            
            return CheckResult(Status.OK, f"Temps OK (max {max_temp:.1f}°C)")
        except Exception as e:
            return CheckResult(Status.UNKNOWN, f"Sensor error: {e}")


class FilesystemCheck(SystemCheck):
    """Check ZFS/BTRFS status."""
    
    def __init__(self):
        super().__init__()
        self._zpool_available = shutil.which("zpool") is not None
        self._btrfs_available = shutil.which("btrfs") is not None

    @property
    def name(self) -> str:
        return "Filesystems"

    async def _execute(self) -> CheckResult:
        issues = []
        
        if self._zpool_available:
            code, stdout, _ = await self._run_cmd(["zpool", "status", "-x"])
            if code == 0 and stdout and "healthy" not in stdout.lower():
                issues.append("ZFS pool unhealthy")
        
        if self._btrfs_available:
            code, stdout, _ = await self._run_cmd(["btrfs", "filesystem", "show"])
            if code != 0 or "error" in stdout.lower():
                issues.append("BTRFS error")

        if issues:
            return CheckResult(Status.WARNING, "Filesystem issues", details=tuple(issues))
        return CheckResult(Status.OK, "Filesystems healthy")


class NetworkCheck(SystemCheck):
    """Check network connectivity."""
    
    def __init__(self):
        super().__init__(timeout=3.0)

    @property
    def name(self) -> str:
        return "Network"

    async def _execute(self) -> CheckResult:
        # Use asyncio-based check instead of ping subprocess for speed
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("8.8.8.8", 53),
                timeout=self.timeout
            )
            writer.close()
            await writer.wait_closed()
            return CheckResult(Status.OK, "Internet connected")
        except asyncio.TimeoutError:
            return CheckResult(Status.WARNING, "Internet unreachable (timeout)")
        except OSError as e:
            return CheckResult(Status.WARNING, f"Network error: {e}")


class AuditCheck(SystemCheck):
    """Check security audit logs."""
    
    def __init__(self):
        super().__init__()
        self._ausearch_available = shutil.which("ausearch") is not None

    @property
    def name(self) -> str:
        return "Audit"

    async def _execute(self) -> CheckResult:
        if not self._ausearch_available:
            return CheckResult(Status.UNKNOWN, "ausearch not installed")

        code, stdout, _ = await self._run_cmd([
            "ausearch", "-m", "avc", "-ts", "recent"
        ])
        
        if code == 0 and stdout:
            denials = len([l for l in stdout.splitlines() if l.strip()])
            if denials > 0:
                return CheckResult(
                    Status.WARNING,
                    f"{denials} SELinux denial(s)",
                    metrics={"denials": denials}
                )
        
        return CheckResult(Status.OK, "No SELinux denials")

class BtrfsScrubCheck(SystemCheck):
    """Check BTRFS scrub and balance status."""
    
    @property
    def name(self) -> str:
        return "BTRFS Scrub"
    
    async def _execute(self) -> CheckResult:
        if not shutil.which("btrfs"):
            return CheckResult(Status.UNKNOWN, "btrfs-progs not installed")
        
        issues = []
        
        # Check scrub status for all mounted btrfs filesystems
        code, stdout, _ = await self._run_cmd(["btrfs", "filesystem", "show", "--mounted"])
        if code != 0:
            return CheckResult(Status.UNKNOWN, "Cannot list BTRFS filesystems")
        
        for line in stdout.splitlines():
            if "uuid" in line.lower():
                # Extract mountpoint or UUID
                mount = line.split()[-1] if line.split() else "/"
                
                # Check scrub status
                _, scrub_out, _ = await self._run_cmd(["btrfs", "scrub", "status", mount])
                if "no stats available" not in scrub_out and "finished" not in scrub_out:
                    if "running" in scrub_out:
                        issues.append(f"{mount}: scrub running")
                    elif "error" in scrub_out.lower():
                        issues.append(f"{mount}: scrub errors!")
                
                # Check device stats for corruption
                _, stats_out, _ = await self._run_cmd(["btrfs", "device", "stats", mount])
                corruption = [l for l in stats_out.splitlines() if not l.endswith("0")]
                if corruption:
                    issues.append(f"{mount}: {len(corruption)} device errors")
        
        if issues:
            return CheckResult(Status.WARNING, "BTRFS attention needed", details=tuple(issues))
        return CheckResult(Status.OK, "BTRFS healthy")

class PacmanLogCheck(SystemCheck):
    """Analyze recent pacman activity for issues."""
    
    @property
    def name(self) -> str:
        return "Pacman Log"
    
    async def _execute(self) -> CheckResult:
        log_path = Path("/var/log/pacman.log")
        if not log_path.exists():
            return CheckResult(Status.UNKNOWN, "No pacman log")
        
        try:
            # Check last 50 lines for errors
            code, stdout, _ = await self._run_cmd(["tail", "-n", "50", str(log_path)])
            if code != 0:
                return CheckResult(Status.UNKNOWN, "Cannot read log")
            
            errors = []
            for line in stdout.splitlines():
                if any(x in line for x in ["error", "failed", "warning:", "could not"]):
                    errors.append(line.split("] ", 1)[-1][:50])  # Trim timestamp
            
            # Check for partial upgrade (pacman db lock)
            if Path("/var/lib/pacman/db.lck").exists():
                errors.append("Pacman database locked")
            
            if errors:
                return CheckResult(
                    Status.WARNING if len(errors) < 3 else Status.CRITICAL,
                    f"{len(errors)} recent issues",
                    details=tuple(errors[-3:])
                )
            return CheckResult(Status.OK, "Pacman healthy")
        except Exception as e:
            return CheckResult(Status.UNKNOWN, str(e))


class AurUpdateCheck(SystemCheck):
    """Check for AUR updates using yay/paru."""
    
    def __init__(self):
        super().__init__(timeout=15.0)  # AUR checks are slower
        self.helper = None
        for h in ["yay", "paru"]:
            if shutil.which(h):
                self.helper = h
                break

    @property
    def name(self) -> str:
        return "AUR Updates"

    async def _execute(self) -> CheckResult:
        if not self.helper:
            return CheckResult(Status.UNKNOWN, "No AUR helper found")
        
        # Check AUR updates only (not repo)
        code, stdout, _ = await self._run_cmd([self.helper, "-Qua"])
        
        if code != 0:
            return CheckResult(Status.UNKNOWN, f"{self.helper} failed")
        
        count = len([l for l in stdout.splitlines() if l.strip()])
        
        if count > 20:
            return CheckResult(Status.WARNING, f"{count} AUR updates", metrics={"count": count})
        elif count > 0:
            return CheckResult(Status.OK, f"{count} AUR updates", metrics={"count": count})
        return CheckResult(Status.OK, "AUR up to date", metrics={"count": 0})

class SystemdTimerCheck(SystemCheck):
    """Check for failed/missed systemd timers."""
    
    @property
    def name(self) -> str:
        return "Systemd Timers"
    
    async def _execute(self) -> CheckResult:
        code, stdout, _ = await self._run_cmd([
            "systemctl", "list-timers", "--all", "--no-legend", "--failed"
        ])
        
        if code != 0:
            return CheckResult(Status.UNKNOWN, "Cannot query timers")
        
        failed = [l.split()[0] for l in stdout.splitlines() if l.strip()]
        
        # Also check for timers that haven't run recently (stuck)
        code, stdout, _ = await self._run_cmd([
            "systemctl", "list-timers", "--all", "--no-legend"
        ])
        stuck = []
        for line in stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and "n/a" in parts[2]:  # LAST column
                stuck.append(parts[0])
        
        issues = failed + stuck
        
        if issues:
            return CheckResult(
                Status.WARNING,
                f"{len(issues)} timer issue(s)",
                details=tuple(issues[:5])
            )
        return CheckResult(Status.OK, "All timers healthy")

class BuildEnvCheck(SystemCheck):
    """Check development/build environment health."""
    
    @property
    def name(self) -> str:
        return "Build Environment"
    
    async def _execute(self) -> CheckResult:
        issues = []
        
        # Check for orphaned packages
        code, stdout, _ = await self._run_cmd(["pacman", "-Qdtq"])
        if code == 0 and stdout:
            orphans = len(stdout.splitlines())
            if orphans > 10:
                issues.append(f"{orphans} orphaned packages")
        
        # Check pkgcache size (can fill up disk)
        cache_dir = Path("/var/cache/pacman/pkg")
        if cache_dir.exists():
            try:
                total_size = sum(f.stat().st_size for f in cache_dir.glob("*.pkg.tar*"))
                size_gb = total_size / (1024**3)
                if size_gb > 5:
                    issues.append(f"Package cache: {size_gb:.1f}GB")
            except PermissionError:
                pass
        
        # Check for failed builds in ~/build or /tmp
        for build_dir in [Path.home() / "build", Path("/tmp/makepkg")]:
            if build_dir.exists():
                failed_builds = list(build_dir.glob("**/build-failed*"))
                if failed_builds:
                    issues.append(f"{len(failed_builds)} failed builds in {build_dir}")
        
        if issues:
            return CheckResult(Status.WARNING, "Build env issues", details=tuple(issues))
        return CheckResult(Status.OK, "Build env clean")

class MirrorStatusCheck(SystemCheck):
    """Check Arch mirror status via local pacman database age."""

    def __init__(self):
        super().__init__(timeout=5.0)
        # Cache results for 1 hour to avoid rate limiting
        self._cache_ttl = timedelta(seconds=3600)

    @property
    def name(self) -> str:
        return "Mirror Status"

    async def _execute(self) -> CheckResult:
        """Check mirror status by examining pacman sync database age."""
        from datetime import datetime

        # Check when pacman databases were last synced
        sync_dir = Path("/var/lib/pacman/sync")

        if not sync_dir.exists():
            return CheckResult(Status.UNKNOWN, "No sync database")

        try:
            # Get the newest db file modification time
            db_files = list(sync_dir.glob("*.db"))
            if not db_files:
                return CheckResult(Status.UNKNOWN, "No database files")

            newest_mtime = max(f.stat().st_mtime for f in db_files)
            age_hours = (datetime.now().timestamp() - newest_mtime) / 3600

            # Check mirror list exists
            mirrorlist = Path("/etc/pacman.d/mirrorlist")
            if not mirrorlist.exists():
                return CheckResult(Status.WARNING, "No mirrorlist configured")

            # Determine status based on database age
            if age_hours > 168:  # 7 days
                return CheckResult(
                    Status.CRITICAL,
                    f"Database {age_hours/24:.0f} days old",
                    details=("Run: sudo pacman -Sy",)
                )
            elif age_hours > 24:  # 1 day
                return CheckResult(
                    Status.WARNING,
                    f"Database {age_hours:.0f} hours old",
                    details=("Consider updating: sudo pacman -Sy",)
                )
            else:
                return CheckResult(
                    Status.OK,
                    f"Up to date ({age_hours:.1f}h ago)"
                )

        except (OSError, IOError) as e:
            return CheckResult(Status.UNKNOWN, f"Cannot check mirrors: {e}")

class InitramfsCheck(SystemCheck):
    """Check if initramfs/UKI matches kernel version."""

    @property
    def name(self) -> str:
        return "Initramfs"

    async def _execute(self) -> CheckResult:
        running = os.uname().release
        issues = []

        # Check for traditional initramfs files
        initramfs = Path("/boot/initramfs-linux.img")
        fallback = Path("/boot/initramfs-linux-fallback.img")

        # Check for Unified Kernel Images (UKI) - used by Omarchy with Limine
        uki_dir = Path("/boot/EFI/Linux")
        has_uki = False

        if uki_dir.exists():
            # Look for UKI files matching the running kernel
            kernel_variant = ""
            if "-zen" in running:
                kernel_variant = "linux-zen"
            elif "-lts" in running:
                kernel_variant = "linux-lts"
            elif "-hardened" in running:
                kernel_variant = "linux-hardened"
            else:
                kernel_variant = "linux"

            # UKI files are named like: omarchy_linux.efi or omarchy_linux-zen.efi
            uki_files = list(uki_dir.glob(f"*{kernel_variant}*.efi"))

            if uki_files:
                has_uki = True
                # Check if UKI is newer than kernel (indicates it was built for current kernel)
                kernel_path = Path(f"/boot/vmlinuz-{kernel_variant}")
                if kernel_path.exists():
                    for uki in uki_files:
                        if uki.stat().st_mtime < kernel_path.stat().st_mtime:
                            issues.append(f"UKI older than kernel: {uki.name}")
            else:
                issues.append(f"Missing UKI for {kernel_variant}")

        # If no UKI, check for traditional initramfs
        if not has_uki and not initramfs.exists():
            issues.append(f"Missing initramfs for {running}")

        # Check if modules are available for running kernel
        modules_dir = Path(f"/lib/modules/{running}")
        if not modules_dir.exists():
            issues.append(f"Missing modules for running kernel!")

        # Check for mkinitcpio preset errors
        log_path = Path("/var/log/mkinitcpio.log")
        if log_path.exists():
            code, stdout, _ = await self._run_cmd(["tail", "-n", "5", str(log_path)])
            if "error" in stdout.lower():
                issues.append("Recent mkinitcpio errors")

        if issues:
            return CheckResult(Status.CRITICAL, "Boot config issues!", details=tuple(issues))
        return CheckResult(Status.OK, "Boot files valid")



class IntegrityMonitor:
    """Orchestrates all system checks."""
    
    CHECKS: List[type[SystemCheck]] = [
        SystemdCheck,
        DiskHealthCheck,
        UpdatesCheck,
        SecurityCheck,
        SystemErrorsCheck,
        DiskSpaceCheck,
        MemoryCheck,
        CpuCheck,
        TemperatureCheck,
        FilesystemCheck,
        NetworkCheck,
        AuditCheck,
        BtrfsScrubCheck,
        PacmanLogCheck,
        AurUpdateCheck,
        SystemdTimerCheck,
        BuildEnvCheck,
        MirrorStatusCheck,
        InitramfsCheck,
    ]

    def __init__(self):
        self.checks = [cls() for cls in self.CHECKS]
        self._last_run: Optional[datetime] = None

    async def run_all(self) -> Dict[str, CheckResult]:
        """Run all checks concurrently."""
        tasks = [check.run() for check in self.checks]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        output = {}
        for check, result in zip(self.checks, results):
            if isinstance(result, Exception):
                logger.error(f"Check {check.name} crashed: {result}")
                output[check.name] = CheckResult(
                    Status.UNKNOWN,
                    f"Crash: {type(result).__name__}"
                )
            else:
                output[check.name] = result
        
        self._last_run = datetime.now()
        return output

    def get_overall_status(self, results: Dict[str, CheckResult]) -> Status:
        """Determine overall system status."""
        if any(r.status == Status.CRITICAL for r in results.values()):
            return Status.CRITICAL
        if any(r.status == Status.WARNING for r in results.values()):
            return Status.WARNING
        if all(r.status == Status.UNKNOWN for r in results.values()):
            return Status.UNKNOWN
        return Status.OK


class WaybarFormatter:
    """Formats check results for Waybar JSON output."""
    
    TOOLTIP_WIDTH = 60
    
    def __init__(self):
        self.integrity_icons = {
            Status.OK: "󰗠",
            Status.WARNING: "󰞀",
            Status.CRITICAL: "󰍁",
            Status.UNKNOWN: "󰈡",
        }

    def format(self, results: Dict[str, CheckResult]) -> Dict[str, Any]:
        """Format results for Waybar."""
        status_counts = {s: 0 for s in Status}
        for r in results.values():
            status_counts[r.status] += 1
        
        overall = self._determine_overall(status_counts)
        
        return {
            "text": self._format_text(overall, status_counts),
            "tooltip": self._format_tooltip(results, overall, status_counts),
            "markup": "pango",
            "class": f"system-integrity {overall.color_key}"
        }

    def _determine_overall(self, counts: Dict[Status, int]) -> Status:
        if counts[Status.CRITICAL] > 0:
            return Status.CRITICAL
        if counts[Status.WARNING] > 0:
            return Status.WARNING
        if counts[Status.OK] > 0:
            return Status.OK
        return Status.UNKNOWN

    def _format_text(self, overall: Status, counts: Dict[Status, int]) -> str:
        icon = self.integrity_icons[overall]
        issues = counts[Status.WARNING] + counts[Status.CRITICAL]
        
        if issues > 0:
            return f"{icon} <span foreground='{COLORS[overall.color_key]}'>{issues}</span>"
        return icon

    def _format_tooltip(
        self,
        results: Dict[str, CheckResult],
        overall: Status,
        counts: Dict[Status, int]
    ) -> str:
        lines = []
        border = f"<span foreground='{COLORS['bright_black']}'>{'─' * self.TOOLTIP_WIDTH}</span>"
        
        # Header
        icon = self.integrity_icons[overall]
        lines.append(
            f"<span foreground='{COLORS[overall.color_key]}'>{icon}</span> "
            f"<span foreground='{COLORS['white']}'><b>System Integrity</b></span>"
        )
        lines.append(
            f"<span foreground='{COLORS['bright_black']}'>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</span>"
        )
        lines.append(border)
        
        # Summary
        lines.append(
            f"<span foreground='{COLORS[overall.color_key]}'>{overall.icon}</span> "
            f"<b>Status:</b> <span foreground='{COLORS[overall.color_key]}'>{overall.label}</span>"
        )
        lines.append(
            f"   {counts[Status.OK]} OK | {counts[Status.WARNING]} Warn | "
            f"{counts[Status.CRITICAL]} Crit | {counts[Status.UNKNOWN]} Unknown"
        )
        lines.append("")
        
        # Details
        for name, result in results.items():
            color = COLORS[result.status.color_key]
            lines.append(
                f"<span foreground='{color}'>{result.status.icon}</span> "
                f"<b>{name}:</b> <span foreground='{color}'>{result.status.label}</span>"
            )
            
            if result.status != Status.OK:
                if result.message:
                    lines.append(f"   <span foreground='{COLORS['bright_black']}'>└─ {result.message}</span>")
                for detail in result.details[:3]:
                    lines.append(f"   <span foreground='{COLORS['bright_black']}'>└─ {detail}</span>")
        
        lines.append(border)
        lines.append("<span>󰍽 LMB: Refresh  │  RMB: Copy issues</span>")
        
        return f"<span size='12000'>{'\n'.join(lines)}</span>"

    def format_notification(
        self,
        results: Dict[str, CheckResult],
        overall: Status
    ) -> Tuple[str, str, str]:
        """Format for desktop notification. Returns (title, body, urgency)."""
        counts = {s: sum(1 for r in results.values() if r.status == s) for s in Status}
        
        title = f"System Integrity - {overall.label}"
        
        lines = [f"{overall.icon} Overall: {overall.label}"]
        lines.append(f"{counts[Status.OK]} OK | {counts[Status.WARNING]} Warnings | {counts[Status.CRITICAL]} Critical")
        lines.append("")
        
        # Add problem details
        for name, result in results.items():
            if result.status != Status.OK and result.message:
                lines.append(f"• {name}: {result.message}")
        
        urgency = "critical" if overall == Status.CRITICAL else \
                  "normal" if overall == Status.WARNING else "low"
        
        return title, "\n".join(lines), urgency


class NotificationManager:
    """Handles desktop notifications."""
    
    async def send(self, title: str, message: str, urgency: str = "normal") -> None:
        """Send notification via notify-send."""
        if not shutil.which("notify-send"):
            logger.warning("notify-send not available")
            return
        
        try:
            proc = await asyncio.create_subprocess_exec(
                "notify-send",
                "-u", urgency,
                "-t", "10000",
                title,
                message,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.wait()
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")


async def main():
    parser = argparse.ArgumentParser(description="Waybar System Integrity Module")
    parser.add_argument(
        "--quick-check",
        action="store_true",
        help="Run check with notification"
    )
    parser.add_argument(
        "--copy-issues",
        action="store_true",
        help="Copy current warnings/errors to clipboard"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    monitor = IntegrityMonitor()
    formatter = WaybarFormatter()
    
    if args.copy_issues:
        results = await monitor.run_all()
        lines = []
        for name, result in results.items():
            if result.status != Status.OK:
                lines.append(f"[{result.status.label}] {name}: {result.message or ''}")
                for detail in result.details:
                    lines.append(f"  └─ {detail}")

                # For failed systemd services, fetch journal output for each
                if name == "Systemd Services" and result.details:
                    for svc in result.details:
                        lines.append(f"\n── {svc} ──")
                        proc = await asyncio.create_subprocess_exec(
                            "systemctl", "status", svc, "--no-pager", "-l",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        stdout, _ = await proc.communicate()
                        lines.append(stdout.decode("utf-8", errors="replace").strip())
                        lines.append("")
                        proc2 = await asyncio.create_subprocess_exec(
                            "journalctl", "-u", svc, "-n", "30", "--no-pager",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        stdout2, _ = await proc2.communicate()
                        lines.append(stdout2.decode("utf-8", errors="replace").strip())

        text = "\n".join(lines) if lines else "No issues detected"

        # Copy to clipboard — prefer wl-copy (Wayland), fall back to xclip/xsel
        copied = False
        for cmd in [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]:
            if shutil.which(cmd[0]):
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.communicate(input=text.encode())
                copied = True
                break

        notif_body = f"Copied {len(lines)} issue(s) to clipboard" if lines else "No issues to copy"
        await NotificationManager().send("System Integrity", notif_body, "low")

    elif args.quick_check:
        # Loading state
        print(json.dumps({
            "text": "⏳",
            "tooltip": "<span size='12000'>Running system integrity check...</span>",
            "markup": "pango",
            "class": "system-integrity loading"
        }))
        sys.stdout.flush()
        
        # Run checks
        results = await monitor.run_all()
        overall = formatter._determine_overall(
            {s: sum(1 for r in results.values() if r.status == s) for s in Status}
        )
        
        # Send notification
        title, body, urgency = formatter.format_notification(results, overall)
        await NotificationManager().send(title, body, urgency)
    else:
        # Standard waybar output
        results = await monitor.run_all()
        output = formatter.format(results)
        print(json.dumps(output))



if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        # Output valid JSON even on crash
        print(json.dumps({
            "text": "󰈡",
            "tooltip": f"<span foreground='red'>Module error: {e}</span>",
            "markup": "pango",
            "class": "system-integrity error"
        }))
        sys.exit(1)
