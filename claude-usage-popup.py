#!/usr/bin/env python3
"""
Claude Code Usage Popup

A GTK4 floating window for GNOME — shows the same usage stats as the
Waybar module but triggered via a keyboard shortcut instead of a bar.

Install:
    mkdir -p ~/.local/share/claude-usage
    cp claude-usage-popup.py  ~/.local/share/claude-usage/popup.py
    cp waybar-claude-fetch.py ~/.local/share/claude-usage/fetch.py
    chmod +x ~/.local/share/claude-usage/popup.py
    ln -sf ~/.local/share/claude-usage/popup.py ~/.local/bin/claude-usage-popup

Then add a GNOME keyboard shortcut:
    Settings → Keyboard → View and Customise Shortcuts → Custom Shortcuts
    Command: claude-usage-popup
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
from typing import Optional

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

# =============================================================================
# CONFIG
# =============================================================================

CACHE_FILE   = Path("/tmp/waybar_claude_usage.json")
LOCK_FILE    = Path("/tmp/waybar_claude_fetch.lock")
FETCH_SCRIPT = Path(__file__).parent / "fetch.py"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
TOKEN_CACHE  = Path("/tmp/waybar_claude_tokens.json")

CACHE_TTL  = 90   # seconds before triggering background refresh
TOKEN_TTL  = 120  # seconds between token recomputation
BAR_WIDTH  = 20   # characters in progress bar

# =============================================================================
# COLORS  (no omarchy theme on GNOME — hardcoded palette)
# =============================================================================

C_BG      = "#1e1e2e"
C_WHITE   = "#cdd6f4"
C_DIM     = "#6c7086"
C_GREEN   = "#a6e3a1"
C_YELLOW  = "#f9e2af"
C_RED     = "#f38ba8"
C_CYAN    = "#89dceb"
C_MAGENTA = "#cba6f7"

# =============================================================================
# CACHE / FETCH HELPERS
# =============================================================================

def load_cache() -> Optional[dict]:
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return None


def is_stale(data: Optional[dict]) -> bool:
    if data is None:
        return True
    ts = data.get("timestamp", 0) / 1000
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

def usage_color(pct: int) -> str:
    if pct >= 90:
        return C_RED
    if pct >= 75:
        return C_RED
    if pct >= 50:
        return C_YELLOW
    return C_GREEN


def progress_bar(pct: int) -> str:
    pct = max(0, min(100, pct))
    filled = round(pct / 100 * BAR_WIDTH)
    color  = usage_color(pct)
    bar    = (
        f"<span foreground='{color}'>{'█' * filled}</span>"
        f"<span foreground='{C_DIM}'>{'░' * (BAR_WIDTH - filled)}</span>"
    )
    return f"[{bar}] <span foreground='{color}'>{pct}%</span>"


def format_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}K"
    return f"{n / 1_000_000:.1f}M"


def _parse_reset_dt(reset_str: str) -> Optional[datetime]:
    tz_match = re.search(r'\(([\w/]+)\)', reset_str)
    tz_name  = tz_match.group(1) if tz_match else "UTC"
    clean    = re.sub(r'\s*\(.*\)', '', reset_str).strip()
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
                        f"{fmt} %Y-%m-%d",
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


def format_reset_display(reset_str: str) -> str:
    if not reset_str:
        return ""
    dt = _parse_reset_dt(reset_str)
    return dt.strftime("%b %d, %H:%M") if dt else reset_str


def format_reset_compact(reset_str: str) -> str:
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
        total_mins = 0
    h, m2 = divmod(total_mins, 60)
    if h == 0:
        return f"{m2}m"
    return f"{h}h{m2}m" if m2 else f"{h}h"


def _budget_bar(section: Optional[dict]) -> str:
    empty = f"<span foreground='{C_DIM}'>{'▱' * 7}</span>"
    if not section:
        return empty
    reset_str = section.get("resetTime", "")
    if not reset_str:
        return empty
    reset_dt = _parse_reset_dt(reset_str)
    if reset_dt is None:
        return empty
    tz_match = re.search(r'\(([\w/]+)\)', reset_str)
    try:
        from zoneinfo import ZoneInfo
        tz  = ZoneInfo(tz_match.group(1) if tz_match else "UTC")
        now = datetime.now(tz)
    except Exception:
        now = datetime.now()
    cycle_start    = reset_dt - timedelta(days=7)
    elapsed        = (now - cycle_start).total_seconds() / 86400
    current_day    = max(1, min(7, int(elapsed) + 1))
    cumulative     = 100.0 / 7 * current_day
    actual         = section.get("percent", 0)
    ratio          = (actual / cumulative * 100) if cumulative > 0 else 0.0
    filled_blocks  = max(0, min(7, round(ratio / 100 * 7)))
    color = C_RED if ratio > 85 else C_YELLOW if ratio > 60 else C_GREEN
    return (
        f"  <span foreground='{color}'>{'▰' * filled_blocks}</span>"
        f"<span foreground='{C_DIM}'>{'▱' * (7 - filled_blocks)}</span>"
        f"  <span foreground='{C_DIM}'>Day {current_day}/7"
        f" · Budget {cumulative:.0f}% · Used {actual}%</span>"
    )

# =============================================================================
# TOKEN ANALYTICS  (today's sessions)
# =============================================================================

_MODEL_PRICING = {
    "claude-opus-4-6":           (15.0,  75.0, 1.875, 18.75),
    "claude-sonnet-4-6":         ( 3.0,  15.0, 0.30,   3.75),
    "claude-haiku-4-5-20251001": ( 0.80,  4.0, 0.08,   1.00),
}


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


def _is_today(ts_str: str, today) -> bool:
    try:
        utc_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return utc_dt.astimezone().date() == today
    except Exception:
        return False


def _short_model(model: str) -> str:
    if "opus"   in model: return "Opus"
    if "sonnet" in model: return "Sonnet"
    if "haiku"  in model: return "Haiku"
    return model


def compute_today_tokens() -> Optional[TokenStats]:
    try:
        cache = json.loads(TOKEN_CACHE.read_text())
        if time.time() - cache.get("timestamp", 0) < TOKEN_TTL:
            fields = set(TokenStats.__dataclass_fields__)
            return TokenStats(**{k: v for k, v in cache.items() if k in fields})
    except Exception:
        pass

    if not PROJECTS_DIR.is_dir():
        return None

    today       = datetime.now().date()
    today_start = datetime.combine(today, datetime.min.time()).timestamp()
    stats       = TokenStats()
    sessions: set[str] = set()

    for f in PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            if f.stat().st_mtime < today_start:
                continue
        except OSError:
            continue
        for line in f.open(encoding="utf-8", errors="replace"):
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
                msg   = entry.get("message", {})
                usage = msg.get("usage")
                if not usage:
                    continue
                stats.input_tokens       += usage.get("input_tokens", 0)
                stats.output_tokens      += usage.get("output_tokens", 0)
                stats.cache_read_tokens  += usage.get("cache_read_input_tokens", 0)
                stats.cache_write_tokens += usage.get("cache_creation_input_tokens", 0)
                stats.message_count      += 1
                model = msg.get("model", "unknown")
                if model not in stats.models:
                    stats.models[model] = {"count": 0, "input": 0, "output": 0,
                                           "cache_read": 0, "cache_write": 0}
                md = stats.models[model]
                md["count"]       += 1
                md["input"]       += usage.get("input_tokens", 0)
                md["output"]      += usage.get("output_tokens", 0)
                md["cache_read"]  += usage.get("cache_read_input_tokens", 0)
                md["cache_write"] += usage.get("cache_creation_input_tokens", 0)
                stu = usage.get("server_tool_use")
                if stu:
                    stats.web_search_count += stu.get("web_search_requests", 0)
                    stats.web_fetch_count  += stu.get("web_fetch_requests", 0)
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
                stats.turn_count       += 1

    stats.session_count = len(sessions)

    try:
        cache_data = {k: getattr(stats, k) for k in TokenStats.__dataclass_fields__}
        cache_data["timestamp"] = time.time()
        TOKEN_CACHE.write_text(json.dumps(cache_data))
    except Exception:
        pass

    return stats if stats.message_count > 0 else None


def estimate_cost(stats: TokenStats) -> float:
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

# =============================================================================
# MARKUP BUILDER
# =============================================================================

def build_markup(data: Optional[dict], fetching: bool) -> str:
    w = C_WHITE
    d = C_DIM
    lines: list[str] = []

    lines.append(f"<span foreground='{w}' size='14000'><b>Claude Code Usage</b></span>")
    lines.append(f"<span foreground='{d}'>{'─' * 40}</span>")

    if data is None:
        msg = "Fetching… first run takes ~20 seconds." if fetching else "No data — click Refresh."
        lines.append(f"\n<span foreground='{d}'>{msg}</span>\n")
        return "\n".join(lines)

    sections = [
        ("session",    "Session  (5h rolling)"),
        ("week",       "Weekly   (all models)"),
        ("weekSonnet", "Weekly   (Sonnet)"),
    ]
    for key, label in sections:
        s = data.get(key)
        if not s:
            continue
        pct   = s.get("percent", 0)
        reset = s.get("resetTime", "")
        lines.append("")
        lines.append(f"<span foreground='{w}'>{label}</span>")
        lines.append(f"  {progress_bar(pct)}")
        if reset:
            compact = format_reset_compact(reset)
            display = format_reset_display(reset)
            lines.append(f"  <span foreground='{d}'>Resets {display}  ↺{compact}</span>")
        if key in ("week", "weekSonnet"):
            lines.append(_budget_bar(s))

    extra = data.get("extra")
    if extra:
        pct   = extra.get("percent", 0)
        spent = extra.get("spent", 0)
        limit = extra.get("limit", 0)
        color = usage_color(pct)
        lines.append("")
        lines.append(f"<span foreground='{w}'>Extra spend</span>")
        lines.append(f"  {progress_bar(pct)}")
        if limit:
            lines.append(f"  <span foreground='{color}'>${spent:.2f} / ${limit:.2f} spent</span>")

    # Today's token analytics
    tokens = compute_today_tokens()
    if tokens:
        lines.append("")
        lines.append(f"<span foreground='{d}'>{'─' * 40}</span>")
        total_msgs = tokens.message_count + tokens.user_msg_count
        lines.append(
            f"<span foreground='{w}'>Today</span>"
            f"  <span foreground='{d}'>{tokens.session_count} sessions"
            f" · {total_msgs} msgs · {tokens.tool_call_count} tools</span>"
        )
        model_parts = [
            f"{_short_model(m)} {tokens.models[m]['count']}"
            for m in sorted(tokens.models, key=lambda x: tokens.models[x]["count"], reverse=True)
        ]
        avg_turn = ""
        if tokens.turn_count > 0:
            avg_s = tokens.turn_duration_ms / tokens.turn_count / 1000
            if avg_s >= 60:
                mins, secs = divmod(int(avg_s), 60)
                avg_turn = f" · avg turn {mins}m{secs}s"
            else:
                avg_turn = f" · avg turn {int(avg_s)}s"
        if model_parts:
            lines.append(f"  <span foreground='{d}'>{' · '.join(model_parts)}{avg_turn}</span>")
        sorted_tools = sorted(tokens.tools.items(), key=lambda x: x[1], reverse=True)[:5]
        if sorted_tools:
            tool_parts = [f"{name} {count}" for name, count in sorted_tools]
            lines.append(f"  <span foreground='{d}'>{' · '.join(tool_parts)}</span>")
        thinking = (
            f"  <span foreground='{C_MAGENTA}'>◆ {tokens.thinking_blocks}</span>"
            if tokens.thinking_blocks else ""
        )
        lines.append(
            f"  <span foreground='{C_CYAN}'>↓ {format_tokens(tokens.input_tokens)} in</span>"
            f"   <span foreground='{C_GREEN}'>↑ {format_tokens(tokens.output_tokens)} out</span>"
            f"{thinking}"
        )
        if tokens.cache_write_tokens > 0:
            ratio_str = f" ({tokens.cache_read_tokens / tokens.cache_write_tokens:.1f}:1)"
        else:
            ratio_str = ""
        lines.append(
            f"  <span foreground='{d}'>Cache: {format_tokens(tokens.cache_read_tokens)} read"
            f" · {format_tokens(tokens.cache_write_tokens)} written{ratio_str}</span>"
        )
        web_parts = []
        if tokens.web_search_count:
            web_parts.append(f"{tokens.web_search_count} searches")
        if tokens.web_fetch_count:
            web_parts.append(f"{tokens.web_fetch_count} fetches")
        if web_parts:
            lines.append(f"  <span foreground='{d}'>Web: {' · '.join(web_parts)}</span>")
        cost = estimate_cost(tokens)
        if cost > 0:
            lines.append(f"  <span foreground='{C_YELLOW}'>Est. cost: ~${cost:.2f}</span>")

    # Footer
    ts       = data.get("timestamp", 0) / 1000
    age      = int(time.time() - ts)
    cached   = "  (cached)" if data.get("fromCache") else ""
    status   = "fetching…" if fetching else f"updated {age}s ago{cached}"
    lines.append("")
    lines.append(f"<span foreground='{d}'>{'─' * 40}</span>")
    lines.append(f"<span foreground='{d}'>{status}  ·  Esc to close</span>")

    return "\n".join(lines)

# =============================================================================
# GTK WINDOW
# =============================================================================

class UsageWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application) -> None:
        super().__init__(application=app)
        self.set_title("Claude Usage")
        self.set_default_size(520, -1)
        self.set_resizable(False)

        # Dark background
        provider = Gtk.CssProvider()
        provider.load_from_string(f"""
            .claude-popup {{
                background-color: {C_BG};
                border-radius: 8px;
            }}
            .refresh-btn {{
                color: {C_DIM};
                background: transparent;
                border: 1px solid {C_DIM};
                border-radius: 4px;
                padding: 2px 10px;
            }}
            .refresh-btn:hover {{
                color: {C_WHITE};
                border-color: {C_WHITE};
            }}
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self.add_css_class("claude-popup")

        # Layout
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        vbox.set_margin_top(20)
        vbox.set_margin_bottom(16)
        vbox.set_margin_start(24)
        vbox.set_margin_end(24)

        self._label = Gtk.Label()
        self._label.set_use_markup(True)
        self._label.set_halign(Gtk.Align.START)
        self._label.set_valign(Gtk.Align.START)
        self._label.set_wrap(False)

        refresh_btn = Gtk.Button(label="↺  Refresh")
        refresh_btn.add_css_class("refresh-btn")
        refresh_btn.set_halign(Gtk.Align.END)
        refresh_btn.set_margin_top(12)
        refresh_btn.connect("clicked", self._on_refresh)

        vbox.append(self._label)
        vbox.append(refresh_btn)
        self.set_child(vbox)

        # Escape to close
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key)
        self.add_controller(key_ctrl)

        self._load_and_render()

    def _load_and_render(self) -> None:
        data     = load_cache()
        fetching = is_fetch_running()
        if is_stale(data) and not fetching:
            spawn_fetch()
            fetching = True
            GLib.timeout_add(2000, self._poll)
        self._label.set_markup(build_markup(data, fetching))

    def _poll(self) -> bool:
        """Poll every 2s while the fetcher runs; stop once it exits."""
        data     = load_cache()
        fetching = is_fetch_running()
        self._label.set_markup(build_markup(data, fetching))
        return fetching  # True = keep polling

    def _on_refresh(self, _btn) -> None:
        CACHE_FILE.unlink(missing_ok=True)
        if not is_fetch_running():
            spawn_fetch()
        self._label.set_markup(build_markup(None, True))
        GLib.timeout_add(2000, self._poll)

    def _on_key(self, _ctrl, keyval, _keycode, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False


class UsageApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="app.claude.usage")

    def do_activate(self) -> None:
        win = self.get_active_window()
        if win:
            win.present()
            return
        UsageWindow(self).present()


if __name__ == "__main__":
    UsageApp().run(None)
