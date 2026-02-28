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

CACHE_FILE     = Path("/tmp/waybar_claude_usage.json")
LOCK_FILE      = Path("/tmp/waybar_claude_fetch.lock")
FETCH_SCRIPT   = Path.home() / ".config/waybar/scripts/waybar-claude-fetch.py"
THEME_PATH     = Path.home() / ".config/omarchy/current/theme/colors.toml"
CACHE_TTL      = 90    # seconds before triggering a background refresh
BAR_WIDTH      = 20    # characters in progress bar
ACTIVITY_TTL   = 3600  # seconds — hide module if no claude activity within this window
HISTORY_FILE   = Path.home() / ".claude" / "history.jsonl"
PROJECTS_DIR   = Path.home() / ".claude" / "projects"
TOKEN_CACHE    = Path("/tmp/waybar_claude_tokens.json")
TOKEN_TTL      = 120   # seconds between token recomputation


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

    header = f"<span foreground='{w}'>Claude Code Usage</span>"
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
        if key in ("week", "weekSonnet"):
            info = compute_budget_info(section)
            if info:
                lines.append(
                    f"  <span foreground='{w}'>Day {info.current_day}/{info.total_days}"
                    f" — Budget: {info.cumulative_budget:.1f}%"
                    f" — Used: {info.actual_percent}%</span>"
                )
                lines.append(f"  {budget_bar_text(info, theme)}")
        if key == "extra" and "spent" in section:
            spent = section["spent"]
            limit = section.get("limit", 0)
            lines.append(f"  <span foreground='{color}'>${spent:.2f} / ${limit:.2f} spent</span>")
        any_shown = True

    if not any_shown:
        lines.append(f"<span foreground='{bb}'>No usage data found in last fetch.</span>")

    # Today's token usage
    tokens = compute_today_tokens()
    if tokens:
        lines.append("")
        lines.append(f"<span foreground='{bb}'>{'─' * 36}</span>")
        total_msgs = tokens.message_count + tokens.user_msg_count
        lines.append(
            f"<span foreground='{w}'>Today</span>"
            f"  <span foreground='{bb}'>{tokens.session_count} sessions"
            f" · {total_msgs} msgs · {tokens.tool_call_count} tools</span>"
        )

        # Models + avg turn
        model_parts = []
        for model in sorted(tokens.models, key=lambda m: tokens.models[m]["count"], reverse=True):
            model_parts.append(f"{_short_model(model)} {tokens.models[model]['count']}")
        avg_turn = ""
        if tokens.turn_count > 0:
            avg_s = tokens.turn_duration_ms / tokens.turn_count / 1000
            if avg_s >= 60:
                mins, secs = divmod(int(avg_s), 60)
                avg_turn = f" · avg turn {mins}m{secs}s"
            else:
                avg_turn = f" · avg turn {int(avg_s)}s"
        if model_parts:
            lines.append(f"  <span foreground='{bb}'>{' · '.join(model_parts)}{avg_turn}</span>")

        # Top tools
        sorted_tools = sorted(tokens.tools.items(), key=lambda x: x[1], reverse=True)[:6]
        if sorted_tools:
            tool_parts = [f"{name} {count}" for name, count in sorted_tools]
            lines.append(f"  <span foreground='{bb}'>{' · '.join(tool_parts)}</span>")

        # Tokens + thinking
        thinking = (
            f"  <span foreground='{theme.magenta}'>◆ {tokens.thinking_blocks}</span>"
            if tokens.thinking_blocks else ""
        )
        lines.append(
            f"  <span foreground='{theme.cyan}'>↓ {format_tokens(tokens.input_tokens)} in</span>"
            f"   <span foreground='{theme.green}'>↑ {format_tokens(tokens.output_tokens)} out</span>"
            f"{thinking}"
        )

        # Cache
        efficiency = ""
        if tokens.cache_write_tokens > 0:
            ratio = tokens.cache_read_tokens / tokens.cache_write_tokens
            efficiency = f" ({ratio:.1f}:1)"
        lines.append(
            f"  <span foreground='{bb}'>Cache: {format_tokens(tokens.cache_read_tokens)} read"
            f" · {format_tokens(tokens.cache_write_tokens)} written{efficiency}</span>"
        )

        # Web
        web_parts = []
        if tokens.web_search_count:
            web_parts.append(f"{tokens.web_search_count} searches")
        if tokens.web_fetch_count:
            web_parts.append(f"{tokens.web_fetch_count} fetches")
        if web_parts:
            lines.append(f"  <span foreground='{bb}'>Web: {' · '.join(web_parts)}</span>")

        # Cost
        cost = estimate_cost(tokens)
        if cost > 0:
            lines.append(f"  <span foreground='{theme.yellow}'>Est. cost: ~${cost:.2f}</span>")

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


@dataclass(frozen=True)
class BudgetInfo:
    current_day: int
    total_days: int
    cumulative_budget: float
    actual_percent: int
    budget_ratio: float
    filled_blocks: int


def compute_budget_info(section: Optional[dict]) -> Optional[BudgetInfo]:
    """Compute weekly budget progress from a usage section with resetTime."""
    if not section:
        return None
    reset_str = section.get("resetTime", "")
    if not reset_str:
        return None

    reset_dt = _parse_reset_dt(reset_str)
    if reset_dt is None:
        return None

    tz_match = re.search(r'\(([\w/]+)\)', reset_str)
    tz_name = tz_match.group(1) if tz_match else "UTC"

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
    except Exception:
        now = datetime.now()

    cycle_start = reset_dt - timedelta(days=7)
    elapsed = (now - cycle_start).total_seconds() / 86400
    current_day = max(1, min(7, int(elapsed) + 1))

    daily_budget = 100.0 / 7
    cumulative_budget = daily_budget * current_day
    actual_percent = section.get("percent", 0)

    if cumulative_budget > 0:
        budget_ratio = (actual_percent / cumulative_budget) * 100
    else:
        budget_ratio = 0.0

    filled_blocks = max(0, min(7, round(budget_ratio / 100 * 7)))

    return BudgetInfo(
        current_day=current_day,
        total_days=7,
        cumulative_budget=cumulative_budget,
        actual_percent=actual_percent,
        budget_ratio=budget_ratio,
        filled_blocks=filled_blocks,
    )


@dataclass
class TokenStats:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    session_count: int = 0
    message_count: int = 0
    user_msg_count: int = 0
    tool_call_count: int = 0
    thinking_blocks: int = 0
    web_search_count: int = 0
    web_fetch_count: int = 0
    turn_duration_ms: int = 0
    turn_count: int = 0
    models: dict = field(default_factory=dict)
    tools: dict = field(default_factory=dict)


# Per-million-token pricing: (input, output, cache_read, cache_write)
_MODEL_PRICING = {
    "claude-opus-4-6":            (15.0,  75.0, 1.875,  18.75),
    "claude-sonnet-4-6":          ( 3.0,  15.0, 0.30,    3.75),
    "claude-haiku-4-5-20251001":  ( 0.80,  4.0, 0.08,    1.00),
}


def _short_model(model: str) -> str:
    if "opus" in model:
        return "Opus"
    if "sonnet" in model:
        return "Sonnet"
    if "haiku" in model:
        return "Haiku"
    return model


def _is_today(ts_str: str, today) -> bool:
    try:
        utc_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return utc_dt.astimezone().date() == today
    except Exception:
        return False


def compute_today_tokens() -> Optional[TokenStats]:
    """Aggregate token usage from today's Claude Code sessions."""
    try:
        cache = json.loads(TOKEN_CACHE.read_text())
        if time.time() - cache.get("timestamp", 0) < TOKEN_TTL:
            fields = set(TokenStats.__dataclass_fields__)
            return TokenStats(**{k: v for k, v in cache.items() if k in fields})
    except Exception:
        pass

    if not PROJECTS_DIR.is_dir():
        return None

    today = datetime.now().date()
    today_start = datetime.combine(today, datetime.min.time()).timestamp()

    stats = TokenStats()
    sessions: set[str] = set()

    for f in PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            if f.stat().st_mtime < today_start:
                continue
        except OSError:
            continue

        for line in f.open(encoding="utf-8", errors="replace"):
            # Skip progress/snapshot noise (~60% of lines)
            if '"_progress"' in line:
                continue
            if '"progress"' in line and '"system"' not in line:
                continue
            if '"file-history-snapshot"' in line or '"queue-operation"' in line:
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts_str = entry.get("timestamp", "")
            if not ts_str or not _is_today(ts_str, today):
                continue

            sid = entry.get("sessionId", "")
            if sid:
                sessions.add(sid)

            entry_type = entry.get("type")

            if entry_type == "assistant":
                msg = entry.get("message", {})
                usage = msg.get("usage")
                if not usage:
                    continue

                stats.input_tokens += usage.get("input_tokens", 0)
                stats.output_tokens += usage.get("output_tokens", 0)
                stats.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                stats.cache_write_tokens += usage.get("cache_creation_input_tokens", 0)
                stats.message_count += 1

                # Model tracking
                model = msg.get("model", "unknown")
                if model not in stats.models:
                    stats.models[model] = {
                        "count": 0, "input": 0, "output": 0,
                        "cache_read": 0, "cache_write": 0,
                    }
                md = stats.models[model]
                md["count"] += 1
                md["input"] += usage.get("input_tokens", 0)
                md["output"] += usage.get("output_tokens", 0)
                md["cache_read"] += usage.get("cache_read_input_tokens", 0)
                md["cache_write"] += usage.get("cache_creation_input_tokens", 0)

                # Web search tracking
                stu = usage.get("server_tool_use")
                if stu:
                    stats.web_search_count += stu.get("web_search_requests", 0)
                    stats.web_fetch_count += stu.get("web_fetch_requests", 0)

                # Content blocks: thinking + tool_use
                for block in msg.get("content", []):
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "thinking":
                        stats.thinking_blocks += 1
                    elif btype == "tool_use":
                        stats.tool_call_count += 1
                        name = block.get("name", "unknown")
                        stats.tools[name] = stats.tools.get(name, 0) + 1

            elif entry_type == "user":
                stats.user_msg_count += 1

            elif entry_type == "system" and entry.get("subtype") == "turn_duration":
                stats.turn_duration_ms += entry.get("durationMs", 0)
                stats.turn_count += 1

    stats.session_count = len(sessions)

    try:
        cache_data = {
            "input_tokens": stats.input_tokens,
            "output_tokens": stats.output_tokens,
            "cache_read_tokens": stats.cache_read_tokens,
            "cache_write_tokens": stats.cache_write_tokens,
            "session_count": stats.session_count,
            "message_count": stats.message_count,
            "user_msg_count": stats.user_msg_count,
            "tool_call_count": stats.tool_call_count,
            "thinking_blocks": stats.thinking_blocks,
            "web_search_count": stats.web_search_count,
            "web_fetch_count": stats.web_fetch_count,
            "turn_duration_ms": stats.turn_duration_ms,
            "turn_count": stats.turn_count,
            "models": stats.models,
            "tools": stats.tools,
            "timestamp": time.time(),
        }
        TOKEN_CACHE.write_text(json.dumps(cache_data))
    except Exception:
        pass

    return stats if stats.message_count > 0 else None


def estimate_cost(stats: TokenStats) -> float:
    """Estimate USD cost from per-model token counts."""
    total = 0.0
    for model, data in stats.models.items():
        pricing = _MODEL_PRICING.get(model)
        if not pricing:
            continue
        inp_p, out_p, cr_p, cw_p = pricing
        total += data["input"]       / 1_000_000 * inp_p
        total += data["output"]      / 1_000_000 * out_p
        total += data["cache_read"]  / 1_000_000 * cr_p
        total += data["cache_write"] / 1_000_000 * cw_p
    return total


def format_tokens(n: int) -> str:
    """Format token count: 823, 1.2K, 45.8K, 1.2M"""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}K"
    return f"{n / 1_000_000:.1f}M"


def budget_color(ratio: float, theme: ColorTheme) -> str:
    """Color for budget bar based on consumption ratio."""
    if ratio > 100:
        return theme.red
    if ratio > 85:
        return theme.bright_red
    if ratio > 60:
        return theme.yellow
    return theme.green


def budget_bar_text(info: Optional[BudgetInfo], theme: ColorTheme) -> str:
    """Render 7-block budget bar: ▰▰▰▱▱▱▱"""
    if info is None:
        return f"<span foreground='{theme.bright_black}'>{'▱' * 7}</span>"

    color = budget_color(info.budget_ratio, theme)
    filled = '▰' * info.filled_blocks
    empty = '▱' * (7 - info.filled_blocks)
    return (
        f"<span foreground='{color}'>{filled}</span>"
        f"<span foreground='{theme.bright_black}'>{empty}</span>"
    )


def build_text(data: Optional[dict], theme: ColorTheme, fetching: bool) -> str:
    bb = theme.bright_black
    bar = budget_bar_text(None, theme)

    if data is None:
        spinner = "…" if fetching else "?"
        return f"<span foreground='{bb}'>{spinner}</span> {bar}"

    session = data.get("session")
    if not session:
        return f"<span foreground='{bb}'>N/A</span> {bar}"

    pct     = session.get("percent", 0)
    color   = usage_color(pct, theme)
    compact = format_reset_compact(session.get("resetTime", ""))
    reset   = f" <span foreground='{bb}'>↺{compact}</span>" if compact else ""

    week = data.get("week")
    info = compute_budget_info(week)
    bar  = budget_bar_text(info, theme)

    return f"<span foreground='{color}'>{pct}%</span>{reset} {bar}"


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
