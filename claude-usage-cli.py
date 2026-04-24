#!/usr/bin/env python3
"""
CLI viewer for Claude Code usage — reads waybar module cache files.

Displays usage percentages, budget info, and today's token stats
using ANSI terminal colors. Zero overhead: only reads cached JSON.

Usage:
    python3 claude-usage-cli.py              # normal output
    python3 claude-usage-cli.py --refresh    # fetch fresh data then display
    python3 claude-usage-cli.py --raw        # dump raw cache JSON
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import re

CACHE_FILE   = Path("/tmp/waybar_claude_usage.json")
TOKEN_CACHE  = Path("/tmp/waybar_claude_tokens.json")
LOCK_FILE    = Path("/tmp/waybar_claude_fetch.lock")
FETCH_SCRIPT = Path.home() / ".config/waybar/scripts/waybar-claude-fetch.py"

# ── ANSI colors ──────────────────────────────────────────────────────────────

RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
RED     = "\033[31m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
BLUE    = "\033[34m"
MAGENTA = "\033[35m"
CYAN    = "\033[36m"
WHITE   = "\033[37m"
BRED    = "\033[91m"
BGREEN  = "\033[92m"
BYELLOW = "\033[93m"
GRAY    = "\033[90m"

BAR_WIDTH = 20


# ── Helpers ──────────────────────────────────────────────────────────────────

def color_for_pct(pct: int) -> str:
    if pct >= 90:
        return RED
    if pct >= 75:
        return BRED
    if pct >= 50:
        return YELLOW
    return GREEN


def progress_bar(pct: int) -> str:
    pct = max(0, min(100, pct))
    filled = round(pct / 100 * BAR_WIDTH)
    empty = BAR_WIDTH - filled
    c = color_for_pct(pct)
    return f"{c}{'█' * filled}{GRAY}{'░' * empty}{RESET} {c}{pct}%{RESET}"


def budget_bar(ratio: float, filled_blocks: int) -> str:
    if ratio > 100:
        c = RED
    elif ratio > 85:
        c = BRED
    elif ratio > 60:
        c = YELLOW
    else:
        c = GREEN
    return f"{c}{'▰' * filled_blocks}{GRAY}{'▱' * (7 - filled_blocks)}{RESET}"


def format_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}K"
    return f"{n / 1_000_000:.1f}M"


def short_model(model: str) -> str:
    if "opus" in model:
        return "Opus"
    if "sonnet" in model:
        return "Sonnet"
    if "haiku" in model:
        return "Haiku"
    if "synthetic" in model:
        return "synth"
    return model[:7]


def _parse_reset_dt(reset_str: str) -> Optional[datetime]:
    """Parse resetTime string into a datetime of the next reset."""
    tz_match = re.search(r'\(([\w/]+)\)', reset_str)
    tz_name = tz_match.group(1) if tz_match else "UTC"
    clean = re.sub(r'\s*\(.*\)', '', reset_str).strip()
    repaired = re.sub(r'(\d+)\s+([ap])\s*m$', r'\1\2m', clean, flags=re.IGNORECASE)
    repaired = re.sub(r'^(\d+)\s+m$', r'\1am', repaired)

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        up = repaired.upper()
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


def format_reset(reset_str: str) -> str:
    """Return human-readable remaining time like '3h45m'."""
    if not reset_str:
        return ""

    clean = re.sub(r'\s*\(.*\)', '', reset_str).strip()
    m = re.match(r'^(\d+)\s*h\s*(\d+)\s*m$', clean, re.IGNORECASE)
    if m:
        return f"{m.group(1)}h{m.group(2)}m"
    m = re.match(r'^(\d+)\s*h$', clean, re.IGNORECASE)
    if m:
        return f"{m.group(1)}h"

    dt = _parse_reset_dt(reset_str)
    if dt is None:
        return ""

    tz_match = re.search(r'\(([\w/]+)\)', reset_str)
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_match.group(1) if tz_match else "UTC")
        total_mins = max(0, int((dt - datetime.now(tz)).total_seconds() / 60))
    except Exception:
        return ""
    h, mins = divmod(total_mins, 60)
    if h == 0:
        return f"{mins}m"
    return f"{h}h{mins}m" if mins else f"{h}h"


# ── Pricing (same as waybar module) ─────────────────────────────────────────

_MODEL_PRICING = {
    "claude-opus-4-6":           (15.0,  75.0, 1.875,  18.75),
    "claude-sonnet-4-6":         ( 3.0,  15.0, 0.30,    3.75),
    "claude-haiku-4-5-20251001": ( 0.80,  4.0, 0.08,    1.00),
}


def estimate_cost(models: dict) -> dict[str, float]:
    costs: dict[str, float] = {}
    total = 0.0
    for model, data in models.items():
        pricing = _MODEL_PRICING.get(model)
        if not pricing:
            continue
        inp_p, out_p, cr_p, cw_p = pricing
        c = (
            data["input"]       / 1_000_000 * inp_p
            + data["output"]    / 1_000_000 * out_p
            + data["cache_read"]  / 1_000_000 * cr_p
            + data["cache_write"] / 1_000_000 * cw_p
        )
        costs[model] = c
        total += c
    costs["_total"] = total
    return costs


# ── Budget ───────────────────────────────────────────────────────────────────

def compute_budget(section: Optional[dict]) -> Optional[dict]:
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
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        now = datetime.now()

    cycle_start = reset_dt - timedelta(days=7)
    current_day = max(1, min(7, (now.date() - cycle_start.date()).days + 1))
    daily_budget = 100.0 / 7
    cumulative = daily_budget * current_day
    actual = section.get("percent", 0)
    ratio = (actual / cumulative * 100) if cumulative > 0 else 0.0
    filled = max(0, min(7, round(ratio / 100 * 7)))

    return {
        "current_day": current_day,
        "cumulative": cumulative,
        "actual": actual,
        "ratio": ratio,
        "filled": filled,
    }


# ── Render ───────────────────────────────────────────────────────────────────

def render(data: dict, tokens: Optional[dict]) -> None:
    sep = f"{GRAY}{'─' * 50}{RESET}"

    print(f"\n{BOLD}{WHITE}  Claude Code Usage{RESET}")
    print(f"  {sep}")

    if data.get("error") == "rate_limited":
        print(f"  {BRED}API rate limited — data may be stale{RESET}")

    sections = [
        ("session",    "Session (5h)"),
        ("week",       "Weekly (all)"),
        ("weekSonnet", "Weekly (Son)"),
        ("extra",      "Extra spend"),
    ]

    for key, label in sections:
        section = data.get(key)
        if not section:
            continue
        pct = section.get("percent", 0)
        reset = format_reset(section.get("resetTime", ""))
        reset_str = f" {GRAY}↺{reset}{RESET}" if reset else ""
        print(f"\n  {WHITE}{label:<14}{RESET}{progress_bar(pct)}{reset_str}")

        if key in ("week", "weekSonnet"):
            info = compute_budget(section)
            if info:
                bc = RED if info["ratio"] > 100 else BRED if info["ratio"] > 85 else YELLOW if info["ratio"] > 60 else GREEN
                print(
                    f"  {' ' * 14}{budget_bar(info['ratio'], info['filled'])}"
                    f" {GRAY}Day {info['current_day']}/7"
                    f" · budget {info['cumulative']:.0f}%"
                    f" · used{RESET} {bc}{info['actual']}%{RESET}"
                )

        if key == "extra" and "spent" in section:
            c = color_for_pct(pct)
            print(f"  {' ' * 14}{c}${section['spent']:.2f} / ${section.get('limit', 0):.2f} spent{RESET}")

    # ── Token stats ──
    if tokens and tokens.get("message_count", 0) > 0:
        total_msgs = tokens.get("message_count", 0) + tokens.get("user_msg_count", 0)
        print(f"\n  {sep}")
        print(
            f"\n  {WHITE}Today{RESET}"
            f" {GRAY}{tokens.get('session_count', 0)} sess"
            f" · {total_msgs} msgs"
            f" · {tokens.get('tool_call_count', 0)} tools{RESET}"
        )

        models = tokens.get("models", {})
        costs = estimate_cost(models)
        sorted_models = sorted(models, key=lambda m: models[m]["count"], reverse=True)

        for model in sorted_models:
            md = models[model]
            if md["count"] == 0:
                continue
            name = short_model(model)
            total_in = md["input"] + md["cache_read"] + md["cache_write"]
            cost_part = ""
            if model in costs:
                cost_part = f" {YELLOW}~${costs[model]:.0f}{RESET}"
            print(
                f"  {GRAY}  {name:<8}{str(md['count']):<5}"
                f"↓{format_tokens(total_in):<7}"
                f"↑{format_tokens(md['output'])}{RESET}"
                f"{cost_part}"
            )

        # Avg turn + thinking
        parts: list[str] = []
        turn_count = tokens.get("turn_count", 0)
        if turn_count > 0:
            avg_s = tokens.get("turn_duration_ms", 0) / turn_count / 1000
            if avg_s >= 60:
                mins, secs = divmod(int(avg_s), 60)
                parts.append(f"avg turn {mins}m{secs}s")
            else:
                parts.append(f"avg turn {int(avg_s)}s")
        thinking = tokens.get("thinking_blocks", 0)
        msg_count = tokens.get("message_count", 0)
        if thinking:
            pct = round(thinking / msg_count * 100) if msg_count else 0
            t = f"◆ {thinking} ({pct}%)"
            chars = tokens.get("thinking_chars", 0)
            if chars:
                t += f" ~{format_tokens(chars // 4)}"
            parts.append(f"{MAGENTA}{t}{RESET}")
        if parts:
            non_mag = [p for p in parts if MAGENTA not in p]
            mag = [p for p in parts if MAGENTA in p]
            line_parts = [f"{GRAY}{p}{RESET}" for p in non_mag] + mag
            print(f"\n  {'  · '.join(line_parts)}")

        # IO totals
        print(
            f"  {CYAN}↓{format_tokens(tokens.get('input_tokens', 0))} in{RESET}"
            f"  {GREEN}↑{format_tokens(tokens.get('output_tokens', 0))} out{RESET}"
        )

        # Cache + web
        cache_parts = []
        cr = tokens.get("cache_read_tokens", 0)
        cw = tokens.get("cache_write_tokens", 0)
        if cr or cw:
            eff = f" ({cr / cw:.1f}:1)" if cw > 0 else ""
            cache_parts.append(f"cache {format_tokens(cr)}r/{format_tokens(cw)}w{eff}")
        tools = tokens.get("tools", {})
        ws = tools.get("WebSearch", 0)
        wf = tools.get("WebFetch", 0)
        if ws or wf:
            web = "web"
            if ws:
                web += f" {ws}s"
            if wf:
                web += f" {wf}f"
            cache_parts.append(web)
        if cache_parts:
            print(f"  {GRAY}{' · '.join(cache_parts)}{RESET}")

        # Top tools
        sorted_tools = sorted(tools.items(), key=lambda x: x[1], reverse=True)[:8]
        if sorted_tools:
            print()
            for i in range(0, len(sorted_tools), 4):
                chunk = sorted_tools[i:i + 4]
                tool_parts = [f"{name} {count}" for name, count in chunk]
                print(f"  {GRAY}{' · '.join(tool_parts)}{RESET}")

        # Cost
        total_cost = costs.get("_total", 0)
        if total_cost > 0:
            print(f"  {YELLOW}~${total_cost:.2f}{RESET}")

    # Footer
    ts = data.get("timestamp", 0) / 1000
    age = int(time.time() - ts)
    cached = " (cached)" if data.get("fromCache") else ""
    print(f"\n  {sep}")
    print(f"  {GRAY}{age}s ago{cached}{RESET}\n")


# ── Refresh ───────────────────────────────────────────────────────────────────

def is_fetch_running() -> bool:
    if not LOCK_FILE.exists():
        return False
    try:
        pid = int(LOCK_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, OSError):
        return False


def refresh_and_wait(timeout: float = 45.0) -> None:
    """Spawn the background fetcher and wait for it to finish."""
    if is_fetch_running():
        print(f"  {GRAY}Fetch already running, waiting…{RESET}", flush=True)
    else:
        print(f"  {GRAY}Fetching fresh data…{RESET}", flush=True)
        # Wipe usage cache so fetcher runs unconditionally
        CACHE_FILE.unlink(missing_ok=True)
        subprocess.Popen(
            [sys.executable, str(FETCH_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    # Wait for the lock file to appear (fetcher started)
    start_deadline = time.time() + 5.0
    while time.time() < start_deadline:
        if is_fetch_running():
            break
        time.sleep(0.2)

    # Wait for the fetcher to finish (lock file disappears)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_fetch_running():
            break
        time.sleep(0.5)
    else:
        print(f"  {YELLOW}Timed out waiting for fetch ({timeout:.0f}s){RESET}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if "--raw" in sys.argv:
        for f in (CACHE_FILE, TOKEN_CACHE):
            if f.exists():
                print(f"── {f} ──")
                print(json.dumps(json.loads(f.read_text()), indent=2))
        return

    if "--refresh" in sys.argv:
        refresh_and_wait()

    if not CACHE_FILE.exists():
        print(f"{GRAY}No cached data yet. The waybar module creates it automatically.{RESET}")
        print(f"{GRAY}If the waybar module is running, data should appear shortly.{RESET}")
        sys.exit(0)

    try:
        data = json.loads(CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"{RED}Failed to read cache: {e}{RESET}", file=sys.stderr)
        sys.exit(1)

    tokens = None
    if TOKEN_CACHE.exists():
        try:
            tokens = json.loads(TOKEN_CACHE.read_text())
        except Exception:
            pass

    render(data, tokens)


if __name__ == "__main__":
    main()
