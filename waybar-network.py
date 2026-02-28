#!/usr/bin/env python3
"""
Waybar Network Bandwidth Module

Shows live download/upload speeds with theme-aware color coding.
Rich tooltip with interface info, WiFi signal, and speed details.

Click actions:
  LMB  -- copy local IP to clipboard + notify
  MMB  -- ping gateway, notify with latency
  RMB  -- fetch & copy public/external IP + notify

Requirements: python3, wl-copy, notify-send, iw (optional), ip
Optional: psutil
"""

import argparse
import json
import os
import re
import subprocess
import time
import pathlib
from typing import Optional

try:
    import tomllib
except ImportError:
    tomllib = None  # type: ignore

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore

STATE_FILE = "/tmp/waybar_network_state.json"
TOOLTIP_WIDTH = 38


# =============================================================================
# THEME COLORS
# =============================================================================

def load_theme_colors() -> dict:
    theme_path = pathlib.Path.home() / ".config/omarchy/current/theme/colors.toml"
    defaults = {
        "black": "#000000", "red": "#ff0000", "green": "#00ff00", "yellow": "#ffff00",
        "blue": "#0000ff", "magenta": "#ff00ff", "cyan": "#00ffff", "white": "#ffffff",
        "bright_black": "#555555", "bright_red": "#ff5555", "bright_green": "#55ff55",
        "bright_yellow": "#ffff55", "bright_blue": "#5555ff", "bright_magenta": "#ff55ff",
        "bright_cyan": "#55ffff", "bright_white": "#ffffff",
    }
    if not tomllib or not theme_path.exists():
        return defaults
    try:
        data = tomllib.loads(theme_path.read_text())
        keys = [
            "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
            "bright_black", "bright_red", "bright_green", "bright_yellow",
            "bright_blue", "bright_magenta", "bright_cyan", "bright_white",
        ]
        for i, key in enumerate(keys):
            val = data.get(f"color{i}", defaults[key])
            if re.match(r"^#[0-9A-Fa-f]{6}$", val):
                defaults[key] = val
    except Exception:
        pass
    return defaults


# =============================================================================
# FORMATTING
# =============================================================================

def format_bytes_short(bps: float) -> str:
    if bps < 1000:
        return f"{bps:>3.0f}B"
    elif bps < 1_000_000:
        return f"{bps/1000:>4.0f}K"
    elif bps < 1_000_000_000:
        return f"{bps/1_000_000:>4.1f}M"
    else:
        return f"{bps/1_000_000_000:>4.1f}G"


def format_bytes_long(bps: float) -> str:
    if bps < 1000:
        return f"{bps:.0f} B/s"
    elif bps < 1_000_000:
        return f"{bps/1000:.1f} KB/s"
    elif bps < 1_000_000_000:
        return f"{bps/1_000_000:.2f} MB/s"
    else:
        return f"{bps/1_000_000_000:.2f} GB/s"


def get_speed_color(bps: float, colors: dict) -> str:
    if bps < 100 * 1024:
        return colors["blue"]
    elif bps < 1024 * 1024:
        return colors["cyan"]
    elif bps < 10 * 1024 * 1024:
        return colors["green"]
    elif bps < 50 * 1024 * 1024:
        return colors["yellow"]
    elif bps < 100 * 1024 * 1024:
        return colors["bright_yellow"]
    else:
        return colors["red"]


def get_speed_class(down_bps: float, up_bps: float) -> str:
    peak = max(down_bps, up_bps)
    if peak >= 10 * 1024 * 1024:
        return "busy"
    elif peak >= 512 * 1024:
        return "active"
    return "idle"


def get_signal_color(pct: int, colors: dict) -> str:
    if pct >= 75:
        return colors["green"]
    elif pct >= 50:
        return colors["yellow"]
    elif pct >= 25:
        return colors["bright_yellow"]
    else:
        return colors["red"]


def signal_bar(pct: int, width: int, colors: dict) -> str:
    filled = round((pct / 100) * width)
    return (
        f"<span foreground='{get_signal_color(pct, colors)}'>{'█' * filled}</span>"
        f"<span foreground='{colors['bright_black']}'>{'░' * (width - filled)}</span>"
    )


def sep(colors: dict, width: int = TOOLTIP_WIDTH) -> str:
    return f"<span foreground='{colors['bright_black']}'>{'─' * width}</span>"


# =============================================================================
# NETWORK DATA
# =============================================================================

def get_active_interface() -> Optional[str]:
    try:
        result = subprocess.run(
            ["ip", "route", "get", "8.8.8.8"],
            capture_output=True, text=True, timeout=2, check=False
        )
        m = re.search(r"dev\s+(\S+)", result.stdout)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def is_wifi(iface: str) -> bool:
    return os.path.exists(f"/sys/class/net/{iface}/wireless")


def get_wifi_info(iface: str) -> dict:
    info: dict = {"ssid": None, "signal_dbm": None, "signal_pct": None,
                  "frequency": None, "rx_rate": None, "tx_rate": None}
    try:
        result = subprocess.run(
            ["iw", "dev", iface, "link"],
            capture_output=True, text=True, timeout=2, check=False
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("SSID:"):
                info["ssid"] = line.split("SSID:", 1)[1].strip()
            elif line.startswith("freq:"):
                try:
                    info["frequency"] = int(line.split("freq:", 1)[1].strip().split()[0])
                except ValueError:
                    pass
            elif line.startswith("signal:"):
                m = re.search(r"(-?\d+)", line)
                if m:
                    dbm = int(m.group(1))
                    info["signal_dbm"] = dbm
                    info["signal_pct"] = max(0, min(100, 2 * (dbm + 100)))
            elif line.startswith("rx bitrate:"):
                m = re.search(r"([\d.]+)\s+MBit", line)
                if m:
                    info["rx_rate"] = float(m.group(1))
            elif line.startswith("tx bitrate:"):
                m = re.search(r"([\d.]+)\s+MBit", line)
                if m:
                    info["tx_rate"] = float(m.group(1))
    except Exception:
        pass
    return info


def get_ip_address(iface: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show", iface],
            capture_output=True, text=True, timeout=2, check=False
        )
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+/\d+)", result.stdout)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def get_gateway() -> Optional[str]:
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=2, check=False
        )
        m = re.search(r"via\s+(\S+)", result.stdout)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def get_net_bytes(iface: str) -> Optional[tuple]:
    if psutil:
        try:
            counters = psutil.net_io_counters(pernic=True)
            if iface in counters:
                c = counters[iface]
                return c.bytes_recv, c.bytes_sent
        except Exception:
            pass
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if f"{iface}:" in line:
                    parts = line.split()
                    return int(parts[1]), int(parts[9])
    except Exception:
        pass
    return None


def load_state() -> Optional[dict]:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def save_state(data: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def notify(title: str, body: str, urgency: str = "normal") -> None:
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-t", "4000", title, body],
            check=False
        )
    except Exception:
        pass


def copy_to_clipboard(text: str) -> bool:
    try:
        subprocess.run(["wl-copy", text], check=True, timeout=3)
        return True
    except Exception:
        return False


# =============================================================================
# CLICK ACTIONS
# =============================================================================

def action_copy_local_ip() -> None:
    """LMB: copy local IP address to clipboard."""
    iface = get_active_interface()
    ip = get_ip_address(iface) if iface else None
    if not ip:
        notify("󰤮 Network", "No IP address found", "critical")
        return
    # Strip CIDR for clipboard
    clean_ip = ip.split("/")[0]
    if copy_to_clipboard(clean_ip):
        notify("󰅆 IP Copied", f"{clean_ip}\n\nInterface: {iface}", "low")
    else:
        notify("󰅖 Copy Failed", "Could not access clipboard (wl-copy)", "critical")


def action_ping_gateway() -> None:
    """MMB: ping gateway and report latency."""
    gw = get_gateway()
    if not gw:
        notify("󰟨 Ping", "No gateway found", "critical")
        return
    try:
        result = subprocess.run(
            ["ping", "-c", "4", "-W", "2", gw],
            capture_output=True, text=True, timeout=12, check=False
        )
        # Parse summary line: "rtt min/avg/max/mdev = 1.2/2.3/3.4/0.5 ms"
        m = re.search(r"rtt.*?=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms", result.stdout)
        loss_m = re.search(r"(\d+)%\s+packet\s+loss", result.stdout)
        loss = int(loss_m.group(1)) if loss_m else 100

        if m and loss < 100:
            mn, avg, mx, mdev = m.group(1), m.group(2), m.group(3), m.group(4)
            if loss > 0:
                urgency = "normal"
                icon = "󰀦"
                status = f"{loss}% packet loss!"
            elif float(avg) < 20:
                urgency = "low"
                icon = "󰄭"
                status = "Excellent"
            elif float(avg) < 60:
                urgency = "low"
                icon = "󰄭"
                status = "Good"
            else:
                urgency = "normal"
                icon = "󰀦"
                status = "High latency"

            notify(
                f"{icon} Gateway {gw}",
                f"{status}\n\nAvg: {avg} ms\nMin: {mn} ms  /  Max: {mx} ms\nJitter: ±{mdev} ms",
                urgency
            )
        else:
            notify("󰀦 Gateway unreachable", f"{gw}\n{loss}% packet loss", "critical")
    except subprocess.TimeoutExpired:
        notify("󰀦 Ping timed out", gw, "critical")


def action_copy_public_ip() -> None:
    """RMB: fetch public IP and copy to clipboard."""
    notify("󰖟 Fetching public IP…", "Please wait", "low")
    services = [
        ["curl", "-s", "--max-time", "4", "https://ifconfig.me"],
        ["curl", "-s", "--max-time", "4", "https://api.ipify.org"],
        ["curl", "-s", "--max-time", "4", "https://icanhazip.com"],
    ]
    pub_ip = None
    for cmd in services:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=6, check=False)
            candidate = r.stdout.strip()
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", candidate):
                pub_ip = candidate
                break
        except Exception:
            continue

    if pub_ip:
        if copy_to_clipboard(pub_ip):
            notify("󰅆 Public IP Copied", pub_ip, "low")
        else:
            notify("󰖟 Public IP", pub_ip, "low")
    else:
        notify("󰅖 Public IP Failed", "Could not reach any lookup service", "critical")


# =============================================================================
# OUTPUT GENERATION
# =============================================================================

def generate_output() -> dict:
    colors = load_theme_colors()

    iface = get_active_interface()
    current_time = time.time()
    current_bytes = get_net_bytes(iface) if iface else None

    down_bps = 0.0
    up_bps = 0.0
    prev = load_state()
    if prev and current_bytes and iface and prev.get("iface") == iface:
        dt = current_time - prev.get("timestamp", current_time)
        if 0.1 < dt < 10:
            dd = current_bytes[0] - prev.get("bytes_recv", 0)
            du = current_bytes[1] - prev.get("bytes_sent", 0)
            if dd >= 0:
                down_bps = dd / dt
            if du >= 0:
                up_bps = du / dt

    if current_bytes and iface:
        save_state({
            "iface": iface,
            "bytes_recv": current_bytes[0],
            "bytes_sent": current_bytes[1],
            "timestamp": current_time,
        })

    wifi = is_wifi(iface) if iface else False
    wifi_info = get_wifi_info(iface) if wifi else {}
    ip_addr = get_ip_address(iface) if iface else None
    gateway = get_gateway() if iface else None

    # =========================================================================
    # BAR TEXT
    # =========================================================================
    if not iface:
        bar_text = f"<span foreground='{colors['red']}'>󰤮 no link</span>"
    else:
        down_col = get_speed_color(down_bps, colors)
        up_col = get_speed_color(up_bps, colors)
        bar_text = (
            f"<span foreground='{down_col}'>↓{format_bytes_short(down_bps)}</span> "
            f"<span foreground='{up_col}'>↑{format_bytes_short(up_bps)}</span>"
        )

    # =========================================================================
    # TOOLTIP
    # =========================================================================
    lines = []

    if not iface:
        lines.append(f"<span foreground='{colors['red']}'>󰤮</span>  <span foreground='{colors['red']}'>Disconnected</span>")
    elif wifi:
        ssid = wifi_info.get("ssid") or iface
        signal_pct = wifi_info.get("signal_pct") or 0
        signal_dbm = wifi_info.get("signal_dbm")
        freq = wifi_info.get("frequency")
        rx_rate = wifi_info.get("rx_rate")
        tx_rate = wifi_info.get("tx_rate")
        band = ("5 GHz" if freq >= 5000 else "2.4 GHz") if freq else ""
        sig_col = get_signal_color(signal_pct, colors)

        lines.append(
            f"<span foreground='{colors['cyan']}'>󰤨</span>  "
            f"<span foreground='{colors['white']}'>{ssid}</span>"
        )
        lines.append(sep(colors))
        bar = signal_bar(signal_pct, 16, colors)
        dbm_str = f" ({signal_dbm} dBm)" if signal_dbm is not None else ""
        lines.append(f" Signal  │ {bar}  <span foreground='{sig_col}'>{signal_pct}%{dbm_str}</span>")
        if band:
            lines.append(f"   Band  │ <span foreground='{colors['bright_cyan']}'>{band}</span>")
        if rx_rate or tx_rate:
            rates = []
            if rx_rate:
                rates.append(f"↓ {rx_rate:.0f} Mbps")
            if tx_rate:
                rates.append(f"↑ {tx_rate:.0f} Mbps")
            lines.append(f"   Link  │ <span foreground='{colors['bright_black']}'>{' / '.join(rates)}</span>")
        lines.append(f"  Iface  │ <span foreground='{colors['bright_black']}'>{iface}</span>")
        if ip_addr:
            lines.append(f"     IP  │ <span foreground='{colors['bright_black']}'>{ip_addr}</span>")
        if gateway:
            lines.append(f"     GW  │ <span foreground='{colors['bright_black']}'>{gateway}</span>")
    else:
        lines.append(
            f"<span foreground='{colors['blue']}'>󰈀</span>  "
            f"<span foreground='{colors['white']}'>{iface}</span>"
        )
        lines.append(sep(colors))
        if ip_addr:
            lines.append(f"     IP  │ <span foreground='{colors['bright_black']}'>{ip_addr}</span>")
        if gateway:
            lines.append(f"     GW  │ <span foreground='{colors['bright_black']}'>{gateway}</span>")

    # Bandwidth section
    lines.append("")
    lines.append(sep(colors))
    down_col = get_speed_color(down_bps, colors)
    up_col = get_speed_color(up_bps, colors)
    lines.append(
        f"<span foreground='{down_col}'>↓</span> Download  │ "
        f"<span foreground='{down_col}'>{format_bytes_long(down_bps)}</span>"
    )
    lines.append(
        f"<span foreground='{up_col}'>↑</span>   Upload  │ "
        f"<span foreground='{up_col}'>{format_bytes_long(up_bps)}</span>"
    )

    lines.append("")
    lines.append(sep(colors))
    lines.append(f"<span foreground='{colors['bright_black']}'>󰍽 LMB: local IP · MMB: ping · RMB: public IP</span>")

    tooltip = "<span size='12000'>" + "\n".join(lines) + "</span>"

    return {
        "text": bar_text,
        "tooltip": tooltip,
        "markup": "pango",
        "class": get_speed_class(down_bps, up_bps),
    }


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Waybar Network Module")
    parser.add_argument("--copy-ip", action="store_true", help="Copy local IP to clipboard")
    parser.add_argument("--ping-gw", action="store_true", help="Ping gateway and notify")
    parser.add_argument("--public-ip", action="store_true", help="Fetch and copy public IP")
    args = parser.parse_args()

    if args.copy_ip:
        action_copy_local_ip()
    elif args.ping_gw:
        action_ping_gateway()
    elif args.public_ip:
        action_copy_public_ip()
    else:
        print(json.dumps(generate_output()))


if __name__ == "__main__":
    main()
