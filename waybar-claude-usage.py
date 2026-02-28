#!/usr/bin/env python3
"""
Waybar Claude Code Usage Module

Reads cached usage data and outputs Waybar JSON instantly.
Spawns a background fetcher when the cache is stale.

Cache: /tmp/waybar_claude_usage.json
Lock:  /tmp/waybar_claude_fetch.lock
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib
except ImportError:
    tomllib = None  # type: ignore


# =============================================================================
# CONFIG
# =============================================================================

CLAUDE_ICON    = "󰧿"
CACHE_FILE     = Path("/tmp/waybar_claude_usage.json")
LOCK_FILE      = Path("/tmp/waybar_claude_fetch.lock")
FETCH_SCRIPT   = Path.home() / ".config/waybar/scripts/waybar-claude-fetch.py"
THEME_PATH     = Path.home() / ".config/omarchy/current/theme/colors.toml"
CACHE_TTL      = 90    # seconds before triggering a background refresh
BAR_WIDTH      = 20    # characters in progress bar
ACTIVITY_TTL   = 3600  # seconds — hide module if no claude activity within this window
HISTORY_FILE   = Path.home() / ".claude" / "history.jsonl"


# =============================================================================
# THEME
# =============================================================================

@dataclass(frozen=True)
class ColorTheme:
    black:          str = "#000000"
    red:            str = "#ff0000"
    green:          str = "#00ff00"
    yellow:         str = "#ffff00"
    blue:           str = "#0000ff"
    magenta:        str = "#ff00ff"
    cyan:           str = "#00ffff"
    white:          str = "#ffffff"
    bright_black:   str = "#555555"
    bright_red:     str = "#ff5555"
    bright_green:   str = "#55ff55"
    bright_yellow:  str = "#ffff55"
    bright_blue:    str = "#5555ff"
    bright_magenta: str = "#ff55ff"
    bright_cyan:    str = "#55ffff"
    bright_white:   str = "#ffffff"

    @classmethod
    def from_omarchy_toml(cls, path: Path) -> "ColorTheme":
        defaults = cls()
        if not tomllib or not path.exists():
            return defaults
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            return cls(
                black=data.get("color0",  defaults.black),
                red=data.get("color1",    defaults.red),
                green=data.get("color2",  defaults.green),
                yellow=data.get("color3", defaults.yellow),
                blue=data.get("color4",   defaults.blue),
                magenta=data.get("color5",defaults.magenta),
                cyan=data.get("color6",   defaults.cyan),
                white=data.get("color7",  defaults.white),
                bright_black=data.get("color8",   defaults.bright_black),
                bright_red=data.get("color9",     defaults.bright_red),
                bright_green=data.get("color10",  defaults.bright_green),
                bright_yellow=data.get("color11", defaults.bright_yellow),
                bright_blue=data.get("color12",   defaults.bright_blue),
                bright_magenta=data.get("color13",defaults.bright_magenta),
                bright_cyan=data.get("color14",   defaults.bright_cyan),
                bright_white=data.get("color15",  defaults.bright_white),
            )
        except Exception:
            return defaults


def get_theme() -> ColorTheme:
    return ColorTheme.from_omarchy_toml(THEME_PATH)


# =============================================================================
# CACHE
# =============================================================================

def load_cache() -> Optional[dict]:
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return None


def is_stale(data: Optional[dict]) -> bool:
    if data is None:
        return True
    ts = data.get("timestamp", 0) / 1000  # ms → s
    return (time.time() - ts) > CACHE_TTL


def is_fetch_running() -> bool:
    if not LOCK_FILE.exists():
        return False
    try:
        pid = int(LOCK_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, OSError):
        return False


def is_claude_active() -> bool:
    """Return True if Claude Code was used within ACTIVITY_TTL seconds."""
    try:
        mtime = HISTORY_FILE.stat().st_mtime
        return (time.time() - mtime) < ACTIVITY_TTL
    except OSError:
        return False


def spawn_fetch() -> None:
    subprocess.Popen(
        [sys.executable, str(FETCH_SCRIPT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# =============================================================================
# FORMATTING
# =============================================================================

def usage_color(pct: Optional[int], theme: ColorTheme) -> str:
    if pct is None:
        return theme.bright_black
    if pct >= 90:
        return theme.red
    if pct >= 75:
        return theme.bright_red
    if pct >= 50:
        return theme.yellow
    return theme.green


def progress_bar(pct: int, theme: ColorTheme) -> str:
    """Unicode progress bar like [████████░░░░░░░░░░░░] 45%"""
    pct = max(0, min(100, pct))
    filled = round(pct / 100 * BAR_WIDTH)
    empty  = BAR_WIDTH - filled
    color  = usage_color(pct, theme)
    bar    = (
        f"<span foreground='{color}'>{'█' * filled}</span>"
        f"<span foreground='{theme.bright_black}'>{'░' * empty}</span>"
    )
    return f"[{bar}] <span foreground='{color}'>{pct}%</span>"


def build_tooltip(data: Optional[dict], theme: ColorTheme, fetching: bool) -> str:
    lines: list[str] = []
    w = theme.white
    bb = theme.bright_black

    header = f"<span foreground='{w}'>{CLAUDE_ICON} Claude Code Usage</span>"
    lines.append(header)
    lines.append(f"<span foreground='{bb}'>{'─' * 36}</span>")

    if data is None:
        lines.append(f"<span foreground='{bb}'>Waiting for first fetch…</span>")
        lines.append(f"<span foreground='{bb}'>This takes ~20 seconds on first run.</span>")
        return "<span size='12000'>" + "\n".join(lines) + "</span>"

    sections = [
        ("session",    "Session  (5h rolling)"),
        ("week",       "Weekly   (all models)"),
        ("weekSonnet", "Weekly   (Sonnet)"),
        ("extra",      "Extra spend"),
    ]

    any_shown = False
    for key, label in sections:
        section = data.get(key)
        if not section:
            continue
        pct   = section.get("percent", 0)
        reset = section.get("resetTime", "")
        color = usage_color(pct, theme)
        lines.append("")
        lines.append(f"<span foreground='{w}'>{label}</span>")
        lines.append(f"  {progress_bar(pct, theme)}")
        if reset:
            lines.append(f"  <span foreground='{bb}'>Resets: {format_reset_display(reset)}</span>")
        if key == "extra" and "spent" in section:
            spent = section["spent"]
            limit = section.get("limit", 0)
            lines.append(f"  <span foreground='{color}'>${spent:.2f} / ${limit:.2f} spent</span>")
        any_shown = True

    if not any_shown:
        lines.append(f"<span foreground='{bb}'>No usage data found in last fetch.</span>")

    # Footer
    ts = data.get("timestamp", 0) / 1000
    age = int(time.time() - ts)
    from_cache = data.get("fromCache", False)
    cache_note = "  (cached)" if from_cache else ""
    status = "fetching…" if fetching else f"updated {age}s ago{cache_note}"

    lines.append("")
    lines.append(f"<span foreground='{bb}'>{'─' * 36}</span>")
    lines.append(f"<span foreground='{bb}'>{status}</span>")
    lines.append(f"<span foreground='{bb}'>LMB: force refresh</span>")

    return "<span size='12000'>" + "\n".join(lines) + "</span>"


def _parse_reset_dt(reset_str: str) -> Optional[datetime]:
    """Parse resetTime string into an aware datetime of the next reset occurrence."""
    tz_match = re.search(r'\(([\w/]+)\)', reset_str)
    tz_name  = tz_match.group(1) if tz_match else "UTC"
    clean    = re.sub(r'\s*\(.*\)', '', reset_str).strip()

    # Repair ANSI-mangled "2 m" (was "2am", 'a' eaten by CSI parser) → "2am"
    repaired = re.sub(r'(\d+)\s+([ap])\s*m$', r'\1\2m', clean, flags=re.IGNORECASE)
    repaired = re.sub(r'^(\d+)\s+m$', r'\1am', repaired)

    try:
        from zoneinfo import ZoneInfo
        tz  = ZoneInfo(tz_name)
        now = datetime.now(tz)
        up  = repaired.upper()

        for fmt in ["%b %d, %I:%M%p", "%b %d, %I%p", "%I:%M%p", "%I%p"]:
            try:
                if fmt in ("%I%p", "%I:%M%p"):
                    dt = datetime.strptime(
                        f"{up} {now.year}-{now.month:02d}-{now.day:02d}",
                        f"{fmt} %Y-%m-%d"
                    )
                else:
                    dt = datetime.strptime(f"{up} {now.year}", f"{fmt} %Y")
                dt = dt.replace(tzinfo=tz)
                if dt <= now:
                    dt += timedelta(days=1)
                return dt
            except ValueError:
                continue
    except Exception:
        pass

    return None


def format_reset_compact(reset_str: str) -> str:
    """Compute time remaining until reset, e.g. '6h', '1h30m', '45m'."""
    if not reset_str:
        return ""

    clean = re.sub(r'\s*\(.*\)', '', reset_str).strip()

    # Explicit relative duration with hours: "1 h 30 m" or "2 h"
    m = re.match(r'^(\d+)\s*h\s*(\d+)\s*m$', clean, re.IGNORECASE)
    if m:
        return f"{m.group(1)}h{m.group(2)}m"
    m = re.match(r'^(\d+)\s*h$', clean, re.IGNORECASE)
    if m:
        return f"{m.group(1)}h"

    dt = _parse_reset_dt(reset_str)
    if dt is None:
        return ""

    from zoneinfo import ZoneInfo
    tz_match = re.search(r'\(([\w/]+)\)', reset_str)
    tz = ZoneInfo(tz_match.group(1) if tz_match else "UTC")
    total_mins = max(0, int((dt - datetime.now(tz)).total_seconds() / 60))
    h, m2 = divmod(total_mins, 60)
    if h == 0:
        return f"{m2}m"
    return f"{h}h{m2}m" if m2 else f"{h}h"


def format_reset_display(reset_str: str) -> str:
    """Return human-readable reset time in 24h, e.g. '02:00' or 'Feb 26, 12:00'."""
    if not reset_str:
        return reset_str

    dt = _parse_reset_dt(reset_str)
    if dt is None:
        return reset_str

    return dt.strftime("%b %d, %H:%M")


def build_text(data: Optional[dict], theme: ColorTheme, fetching: bool) -> str:
    if data is None:
        spinner = "…" if fetching else "?"
        return f"{CLAUDE_ICON} <span foreground='{theme.bright_black}'>{spinner}</span>"

    session = data.get("session")
    if not session:
        return f"{CLAUDE_ICON} <span foreground='{theme.bright_black}'>N/A</span>"

    pct     = session.get("percent", 0)
    color   = usage_color(pct, theme)
    compact = format_reset_compact(session.get("resetTime", ""))
    reset   = f" <span foreground='{theme.bright_black}'>↺{compact}</span>" if compact else ""
    return f"{CLAUDE_ICON} <span foreground='{color}'>{pct}%</span>{reset}"


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    # Handle --refresh click: wipe cache so next poll triggers a fetch
    if "--refresh" in sys.argv:
        try:
            CACHE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        if not is_fetch_running():
            spawn_fetch()
        return

    # Hide entirely when Claude hasn't been used recently
    if not is_claude_active():
        print(json.dumps({"text": "", "class": "claude-usage inactive"}))
        return

    theme    = get_theme()
    data     = load_cache()
    fetching = is_fetch_running()

    # Trigger background refresh if stale and nothing is already running
    if is_stale(data) and not fetching:
        spawn_fetch()
        fetching = True

    output = {
        "text":    build_text(data, theme, fetching),
        "tooltip": build_tooltip(data, theme, fetching),
        "markup":  "pango",
        "class":   "claude-usage",
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
