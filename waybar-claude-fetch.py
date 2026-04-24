#!/usr/bin/env python3
"""
Claude Code Usage Background Fetcher

Spawns claude in a PTY, sends /usage, parses the TUI output,
and writes structured JSON to /tmp/waybar_claude_usage.json.

Uses event-driven waiting on cleaned output so it exits as soon as
the data appears — typically ~6-8 seconds instead of fixed 22s sleeps.
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import subprocess
import sys
import threading
import time
from pathlib import Path


CACHE_FILE  = Path("/tmp/waybar_claude_usage.json")
LOCK_FILE   = Path("/tmp/waybar_claude_fetch.lock")
CLAUDE_PATH = Path.home() / ".local/bin/claude"
EXIT_WAIT   = 2.0  # seconds to wait after /exit before killing


# =============================================================================
# LOCK
# =============================================================================

def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)
            return False  # still running
        except (ProcessLookupError, ValueError, OSError):
            pass          # stale lock
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# =============================================================================
# ANSI CLEANING
# =============================================================================

def clean_ansi(raw: str) -> str:
    s = raw
    s = re.sub(r'\x1b\[\d*C', ' ', s)                    # cursor-right → space
    s = re.sub(r'\x1b\[\d+;\d+H', '\n', s)               # cursor-position → newline
    s = re.sub(r'\x1b\[(\d+)(am|pm)', r'\1\2', s, flags=re.IGNORECASE)  # protect am/pm from CSI eating
    s = re.sub(r'\x1b\[[^A-Za-z]*[A-Za-z]', '', s)       # remaining CSI sequences
    s = re.sub(r'\x1b\][^\x07]*\x07', '', s)         # OSC sequences
    s = re.sub(r'[█▉▊▋▌▍▎▏░▒▓▐▛▜▝▘▗▖▞▟]', '', s)  # block / bar chars
    s = s.replace('\r', '\n').replace('\t', ' ')
    s = re.sub(r' {2,}', ' ', s)
    lines = [l.strip() for l in s.split('\n') if l.strip()]
    return '\n'.join(lines)


# =============================================================================
# PARSING
# =============================================================================

def _clean_reset(rt: str) -> str:
    rt = rt.strip()
    rt = re.sub(r'^[a-z]{1,2}\s+', '', rt, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', rt)


def _ctx_extract(text: str, keyword_pat: str, window: int = 400
                 ) -> tuple[Optional[int], Optional[str]]:
    """Find the LAST occurrence of keyword_pat and return (pct, resetTime) from
    the window following it.  Returns (None, None) if not found."""
    best_pct: Optional[int] = None
    best_reset: Optional[str] = None
    for m in re.finditer(keyword_pat, text, re.IGNORECASE):
        chunk = text[m.start():m.start() + window]
        pm = re.findall(r'(\d+)\s*%\s*used', chunk, re.IGNORECASE)
        if pm:
            best_pct = int(pm[0])
            rm = re.findall(r'Rese\w*\s+([\w\d,: ]+\([\w\/]+\))', chunk, re.IGNORECASE)
            best_reset = _clean_reset(rm[0]) if rm else None
    return best_pct, best_reset


def parse_usage(text: str) -> dict:
    # The /usage TUI renders in two frames (quick initial + updated after local
    # scan), causing percentages to appear multiple times.  Use context-aware
    # extraction keyed on section headers, taking the LAST occurrence so the
    # updated frame wins.  Extra spend has no "% used" in the TUI at all —
    # compute it from spent/limit instead.
    result: dict = {
        "session":    None,
        "week":       None,
        "weekSonnet": None,
        "extra":      None,
        "timestamp":  int(time.time() * 1000),
        "fromCache":  False,
    }

    pct, rt = _ctx_extract(text, r'Cu[\s\w]{0,12}ssion')
    if pct is not None:
        result["session"] = {"percent": pct}
        if rt:
            result["session"]["resetTime"] = rt

    pct, rt = _ctx_extract(text, r'all\s+models')
    if pct is not None:
        result["week"] = {"percent": pct}
        if rt:
            result["week"]["resetTime"] = rt

    pct, rt = _ctx_extract(text, r'[Ss]onnet\s+only')
    if pct is not None:
        result["weekSonnet"] = {"percent": pct}
        if rt:
            result["weekSonnet"]["resetTime"] = rt

    spend_match = re.search(
        r'\$(\d+\.?\d*)\s*/\s*\$(\d+\.?\d*)\s*spent', text, re.IGNORECASE
    )
    if spend_match:
        spent = float(spend_match.group(1))
        limit = float(spend_match.group(2))
        pct_extra = round(spent / limit * 100) if limit > 0 else 0
        result["extra"] = {"percent": pct_extra, "spent": spent, "limit": limit}
        reset_extra = re.search(
            r'spent\s*[·\-–]?\s*Rese\w*\s+([\w\d,: ]+\([\w\/]+\))',
            text, re.IGNORECASE
        )
        if reset_extra:
            result["extra"]["resetTime"] = _clean_reset(reset_extra.group(1))

    return result


# =============================================================================
# PTY FETCH
# =============================================================================

def fetch_via_pty() -> dict:
    if not CLAUDE_PATH.exists():
        raise FileNotFoundError(f"Claude not found at {CLAUDE_PATH}")

    chunks: list[str] = []
    lock = threading.Lock()

    master, slave = pty.openpty()

    env = dict(os.environ)
    env.update({"NO_COLOR": "1", "FORCE_COLOR": "0",
                "TERM": "xterm-256color", "COLUMNS": "120", "LINES": "80"})
    # Prevent "nested session" error if run inside Claude Code
    for var in ("CLAUDECODE", "CLAUDE_SESSION_ID", "ANTHROPIC_CLAUDE_CODE"):
        env.pop(var, None)

    proc = subprocess.Popen(
        [str(CLAUDE_PATH), "--dangerously-skip-permissions"],
        stdin=slave, stdout=slave, stderr=slave,
        close_fds=True, cwd="/tmp", env=env,
    )
    os.close(slave)

    def _read_loop() -> None:
        while True:
            try:
                r, _, _ = select.select([master], [], [], 0.5)
                if r:
                    data = os.read(master, 4096)
                    with lock:
                        chunks.append(data.decode("utf-8", errors="replace"))
            except OSError:
                break

    threading.Thread(target=_read_loop, daemon=True).start()

    def _write(data: bytes) -> None:
        try:
            os.write(master, data)
        except OSError:
            pass

    def _cleaned() -> str:
        with lock:
            return clean_ansi("".join(chunks))

    def _wait_for(pattern: str, timeout: float) -> bool:
        """Wait until pattern appears in cleaned output, or timeout fires."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if re.search(pattern, _cleaned()):
                return True
            time.sleep(0.2)
        return False

    try:
        # Wait for the prompt — "bypass permissions on" signals Claude is ready
        _wait_for(r"bypass permissions", timeout=8.0)
        time.sleep(0.3)

        # Type /usage; autocomplete appears on first Enter, executes on second
        _write(b"/usage")
        time.sleep(0.8)
        _write(b"\r")
        time.sleep(0.8)
        _write(b"\r")

        # Exit as soon as usage data is visible in output
        _wait_for(r"\d+\s*%\s*used", timeout=12.0)
        time.sleep(0.5)

        _write(b"/exit\r")
        time.sleep(EXIT_WAIT)
    except OSError:
        pass

    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    try:
        os.close(master)
    except OSError:
        pass

    cleaned = _cleaned()
    result = parse_usage(cleaned)
    if not result.get("session") and not result.get("week"):
        if re.search(r'rate.limit', cleaned, re.IGNORECASE):
            return {"error": "rate_limited", "timestamp": int(time.time() * 1000)}
    return result


# =============================================================================
# CACHE
# =============================================================================

def load_cache() -> dict | None:
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return None


def save_cache(data: dict) -> None:
    CACHE_FILE.write_text(json.dumps(data, indent=2))


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    if not acquire_lock():
        sys.exit(0)
    try:
        data = fetch_via_pty()
        if data.get("error") == "rate_limited":
            old = load_cache()
            payload = old if old else {}
            payload["error"] = "rate_limited"
            payload["timestamp"] = int(time.time() * 1000)
            save_cache(payload)
        elif data.get("session") or data.get("week"):
            save_cache(data)
        else:
            old = load_cache()
            if old:
                old["fromCache"] = True
                save_cache(old)
    except Exception as e:
        print(f"[waybar-claude-fetch] {e}", file=sys.stderr)
        old = load_cache()
        if old:
            old["fromCache"] = True
            save_cache(old)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
