#!/usr/bin/env python3
# ----------------------------------------------------------------------------
# WAYBAR CPU MODULE - Optimized & Secure Version for Omarchy
# ----------------------------------------------------------------------------
# Features:
# - Real-time CPU monitoring with temperature, frequency, and power
# - Per-core visualization with EMA smoothing
# - Top processes list
# - Zombie process detection (safe reporting only)
# - JSON-based state persistence (secure, no pickle)
# - Non-blocking CPU measurement (< 20ms total execution)
# ----------------------------------------------------------------------------

import json
import psutil
import subprocess
import re
import os
import time
import argparse
from collections import deque
import math
import pathlib
import glob

try:
    import tomllib
except ImportError:
    tomllib = None

# Configuration
CPU_ICON_GENERAL = "\uf2db"
HISTORY_FILE = "/tmp/waybar_cpu_history.json"
POWER_STATE_FILE = "/tmp/waybar_cpu_power_state.json"
TOOLTIP_WIDTH = 50
FAN_PROFILE_FILE = "/tmp/fan-profile"
FAN_PROFILES = {
    "desktop": {"label": "Desktop", "icon": "󰧨", "mintemp": 35, "maxtemp": 75, "minpwm": 70, "maxpwm": 255},
    "gaming":  {"label": "Gaming",  "icon": "󰊗", "mintemp": 35, "maxtemp": 60, "minpwm": 120, "maxpwm": 255},
}

# Remove unused imports: shutil, pickle, signal (security + cleanup)


def read_fan_profile():
    """Read current fan profile, default to desktop"""
    try:
        with open(FAN_PROFILE_FILE, "r") as f:
            profile = f.read().strip()
            if profile in FAN_PROFILES:
                return profile
    except Exception:
        pass
    return "desktop"


def toggle_fan_profile():
    """Toggle fan profile between desktop and gaming, write to file, notify"""
    current = read_fan_profile()
    new_profile = "gaming" if current == "desktop" else "desktop"
    try:
        with open(FAN_PROFILE_FILE, "w") as f:
            f.write(new_profile)
    except Exception:
        send_notification("Fan Profile Error", "Failed to write profile file", "critical")
        return
    info = FAN_PROFILES[new_profile]
    send_notification(
        f"{info['icon']} Fan Profile: {info['label']}",
        f"Curve: {info['minpwm']/255*100:.0f}%→100% over {info['mintemp']}°C→{info['maxtemp']}°C",
    )


def send_notification(title, message, urgency="normal"):
    """Send desktop notification with validated urgency"""
    valid_urgencies = {"low", "normal", "critical"}
    if urgency not in valid_urgencies:
        urgency = "normal"
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-t", "5000", title, message],
            capture_output=True,
            check=False
        )
    except Exception:
        pass


def find_zombie_processes():
    """Find all zombie processes efficiently"""
    zombies = []
    try:
        # Single iteration with minimal attr fetch
        for proc in psutil.process_iter(['pid', 'ppid', 'name', 'status']):
            try:
                if proc.info['status'] == psutil.STATUS_ZOMBIE:
                    zombies.append({
                        'pid': proc.info['pid'],
                        'ppid': proc.info['ppid'],
                        'name': proc.info['name'] or "unknown"
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return zombies


def kill_zombie_processes():
    """Report zombie processes but don't kill parents (they're harmless)"""
    zombies = find_zombie_processes()
    
    if not zombies:
        send_notification(
            "✅ No Zombie Processes",
            "System is clean - no zombie processes found",
            "low"
        )
        return 0, 0, 0
    
    zombie_info = []
    for z in zombies[:5]:
        try:
            parent = psutil.Process(z['ppid'])
            parent_name = parent.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            parent_name = "unknown"
        zombie_info.append(f"PID {z['pid']} ({z['name'][:12]}) ← parent: {parent_name[:15]}")
    
    details = "\n".join(zombie_info)
    if len(zombies) > 5:
        details += f"\n... and {len(zombies) - 5} more"
    
    send_notification(
        f"🧟 {len(zombies)} Zombie Process{'es' if len(zombies) > 1 else ''}",
        f"{details}\n\nZombies are harmless dead processes.\nThey clear when parent reads exit status.",
        "low"
    )
    
    return 0, 0, len(zombies)


def load_theme_colors():
    """Load theme colors with validation"""
    theme_path = pathlib.Path.home() / ".config/omarchy/current/theme/colors.toml"
    defaults = {
        "black": "#000000", "red": "#ff0000", "green": "#00ff00", "yellow": "#ffff00",
        "blue": "#0000ff", "magenta": "#ff00ff", "cyan": "#00ffff", "white": "#ffffff",
        "bright_black": "#555555", "bright_red": "#ff5555", "bright_green": "#55ff55",
        "bright_yellow": "#ffff55", "bright_blue": "#5555ff", "bright_magenta": "#ff55ff",
        "bright_cyan": "#55ffff", "bright_white": "#ffffff"
    }
    
    if not tomllib or not theme_path.exists():
        return defaults
    
    try:
        data = tomllib.loads(theme_path.read_text())
        colors = {}
        for i, key in enumerate([
            "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
            "bright_black", "bright_red", "bright_green", "bright_yellow",
            "bright_blue", "bright_magenta", "bright_cyan", "bright_white"
        ]):
            color_val = data.get(f"color{i}", defaults[key])
            # Validate hex color format
            if re.match(r'^#[0-9A-Fa-f]{6}$', color_val):
                colors[key] = color_val
            else:
                colors[key] = defaults[key]
        return {**defaults, **colors}
    except Exception:
        return defaults


COLORS = load_theme_colors()
SECTION_COLORS = {"CPU": {"icon": COLORS["red"], "text": COLORS["red"]}}

# Fixed color table with continuous ranges (no gaps)
COLOR_TABLE = [
    {"color": COLORS["blue"],           "cpu_gpu_temp": (0, 35),    "cpu_power": (0.0, 30)},
    {"color": COLORS["cyan"],           "cpu_gpu_temp": (35, 45),   "cpu_power": (30.0, 60)},
    {"color": COLORS["green"],          "cpu_gpu_temp": (45, 55),   "cpu_power": (60.0, 90)},
    {"color": COLORS["yellow"],         "cpu_gpu_temp": (55, 65),   "cpu_power": (90.0, 120)},
    {"color": COLORS["bright_yellow"],  "cpu_gpu_temp": (65, 75),   "cpu_power": (120.0, 150)},
    {"color": COLORS["bright_red"],     "cpu_gpu_temp": (75, 85),   "cpu_power": (150.0, 180)},
    {"color": COLORS["red"],            "cpu_gpu_temp": (85, 999),  "cpu_power": (180.0, 9999)}
]


def get_color(value, metric_type):
    """Get color for value with proper boundary handling"""
    if value is None:
        return "#ffffff"
    try:
        value = float(value)
    except (ValueError, TypeError):
        return "#ffffff"
    
    for entry in COLOR_TABLE:
        if metric_type in entry:
            low, high = entry[metric_type]
            # Use half-open intervals to avoid gaps: [low, high)
            if low <= value < high:
                return entry["color"]
    
    # Handle max value (inclusive upper bound for last range)
    last_entry = COLOR_TABLE[-1]
    if metric_type in last_entry and value >= last_entry[metric_type][0]:
        return last_entry["color"]
    
    return COLOR_TABLE[-1]["color"]


def get_cpu_name():
    """Extract CPU name with improved regex for Intel/AMD"""
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if "model name" in line and ":" in line:
                    full_name = line.split(":", 1)[1].strip()
                    # Remove common suffixes for both Intel and AMD
                    short_name = re.sub(r'\s+(\d+-Core\s+Processor|CPU\s+@\s+[\d.]+GHz).*', '', full_name)
                    return short_name.strip()
    except Exception:
        pass
    return "Unknown CPU"


def find_zenpower_hwmon():
    """Find zenpower hwmon path for AMD CPUs"""
    hwmon_base = "/sys/class/hwmon"
    if not os.path.exists(hwmon_base):
        return None
    for hwmon in glob.glob(f"{hwmon_base}/hwmon*"):
        name_file = os.path.join(hwmon, "name")
        try:
            with open(name_file, "r") as f:
                if f.read().strip() == "zenpower":
                    return hwmon
        except Exception:
            continue
    return None


def find_nct6687_hwmon():
    """Find nct6687 hwmon path for motherboard fan headers"""
    hwmon_base = "/sys/class/hwmon"
    if not os.path.exists(hwmon_base):
        return None
    for hwmon in glob.glob(f"{hwmon_base}/hwmon*"):
        name_file = os.path.join(hwmon, "name")
        try:
            with open(name_file, "r") as f:
                if f.read().strip() == "nct6687":
                    return hwmon
        except Exception:
            continue
    return None


def get_cpu_fan_speed(hwmon_path):
    """Read average RPM and PWM% across all active nct6687 fan channels"""
    if not hwmon_path:
        return 0, 0.0
    rpms = []
    pwm_val = 0
    for i in range(1, 9):
        try:
            with open(os.path.join(hwmon_path, f"fan{i}_input"), "r") as f:
                rpm = int(f.read().strip())
                if rpm > 0:
                    rpms.append(rpm)
        except Exception:
            continue
        if pwm_val == 0:
            try:
                with open(os.path.join(hwmon_path, f"pwm{i}"), "r") as f:
                    pwm_val = int(f.read().strip())
            except Exception:
                pass
    if not rpms:
        return 0, 0.0
    avg_rpm = int(sum(rpms) / len(rpms))
    pwm_percent = (pwm_val / 255 * 100) if pwm_val > 0 else 0.0
    return avg_rpm, pwm_percent


def get_zenpower_power(zenpower_path):
    """Read power from zenpower hwmon (returns watts)"""
    total_power = 0.0
    for power_file in glob.glob(f"{zenpower_path}/power*_input"):
        try:
            with open(power_file, "r") as f:
                power_microwatts = int(f.read().strip())
                total_power += power_microwatts / 1_000_000
        except Exception:
            continue
    return total_power


def get_rapl_path():
    """Find RAPL energy counter path"""
    base = "/sys/class/powercap"
    if not os.path.exists(base):
        return None
    paths = glob.glob(f"{base}/*/energy_uj")
    for p in paths:
        if "intel-rapl:0" in p and "subsys" not in p and "dram" not in p:
            return p
        if "package" in p:
            return p
    return paths[0] if paths else None


def get_rapl_max_energy(rapl_path):
    """Get max energy value for overflow detection"""
    max_file = os.path.join(os.path.dirname(rapl_path), "max_energy_range_uj")
    try:
        with open(max_file, "r") as f:
            return int(f.read().strip())
    except Exception:
        return None


def load_history():
    """Load history using JSON (secure, no pickle RCE risk)"""
    try:
        with open(HISTORY_FILE, 'r') as f:
            data = json.load(f)
            return {
                'cpu': deque(data.get('cpu', []), maxlen=TOOLTIP_WIDTH),
                'per_core': {int(k): v for k, v in data.get('per_core', {}).items()}
            }
    except Exception:
        return {'cpu': deque(maxlen=TOOLTIP_WIDTH), 'per_core': {}}


def save_history(cpu_hist, per_core_hist):
    """Save history as JSON (secure)"""
    try:
        with open(HISTORY_FILE, 'w') as f:
            json.dump({
                'cpu': list(cpu_hist),
                'per_core': per_core_hist
            }, f)
    except Exception:
        pass


def load_power_state():
    """Load previous power reading for delta calculation"""
    try:
        with open(POWER_STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return None


def save_power_state(energy_uj, timestamp):
    """Save current power state for next delta calculation"""
    try:
        with open(POWER_STATE_FILE, 'w') as f:
            json.dump({'energy_uj': energy_uj, 'timestamp': timestamp}, f)
    except Exception:
        pass


def calculate_power_nonblocking(rapl_path):
    """
    Calculate power consumption without blocking sleep.
    Uses time delta between script invocations.
    """
    max_energy = get_rapl_max_energy(rapl_path)
    
    try:
        with open(rapl_path, "r") as f:
            current_energy = int(f.read().strip())
    except Exception:
        return 0.0
    
    current_time = time.time()
    prev_state = load_power_state()
    
    if prev_state is None:
        save_power_state(current_energy, current_time)
        return 0.0  # First run, no delta available
    
    prev_energy = prev_state['energy_uj']
    prev_time = prev_state['timestamp']
    time_delta = current_time - prev_time
    
    # Minimum time delta for accurate reading (avoid division by near-zero)
    if time_delta < 0.1:
        return 0.0
    
    energy_delta = current_energy - prev_energy
    
    # Handle overflow
    if energy_delta < 0:
        if max_energy:
            energy_delta = (max_energy - prev_energy) + current_energy
        else:
            # Assume 64-bit counter if max not available
            energy_delta = (2**64 - prev_energy) + current_energy
    
    # Calculate power: energy (joules) / time (seconds) = watts
    power = (energy_delta / 1_000_000) / time_delta
    
    # Sanity check: RAPL can report spurious values on some systems
    if power < 0 or power > 500:
        power = 0.0
    
    save_power_state(current_energy, current_time)
    return power


def get_cpu_percent_fast():
    """
    Get CPU percent without blocking interval.
    Uses psutil's non-blocking mode with history-based calculation.
    """
    # interval=None uses the last sample or returns 0.0 on first call
    total = psutil.cpu_percent(interval=None)
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    return total, per_core


def get_core_color(usage):
    """Get color for core usage"""
    if usage < 20:
        return "#81c8be"
    elif usage < 40:
        return "#a6d189"
    elif usage < 60:
        return "#e5c890"
    elif usage < 80:
        return "#ef9f76"
    elif usage < 95:
        return "#ea999c"
    else:
        return "#e78284"


PROCESS_STATE_FILE = "/tmp/waybar_cpu_proc_state.json"


def load_process_state():
    try:
        with open(PROCESS_STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def save_process_state(state):
    try:
        with open(PROCESS_STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception:
        pass


def get_top_processes(count=3):
    """Get top CPU processes using cross-run state for accurate real-time values."""
    current_time = time.time()
    prev_state = load_process_state()
    current_state = {}
    process_cpu = []
    cpu_count = psutil.cpu_count() or 1

    try:
        for proc in psutil.process_iter(['pid', 'name', 'status']):
            try:
                pid_str = str(proc.info['pid'])
                name = proc.info['name']
                if not name or 'waybar' in name.lower():
                    continue
                if proc.info['status'] == psutil.STATUS_ZOMBIE:
                    continue

                ct = proc.cpu_times()
                total_cpu = ct.user + ct.system
                current_state[pid_str] = {'cpu_total': total_cpu, 'timestamp': current_time}

                if pid_str in prev_state:
                    prev = prev_state[pid_str]
                    time_delta = current_time - prev['timestamp']
                    if time_delta >= 0.5:
                        cpu_delta = total_cpu - prev['cpu_total']
                        if cpu_delta >= 0:
                            cpu_percent = (cpu_delta / time_delta) * 100.0
                            process_cpu.append({'name': name, 'cpu_percent': min(cpu_percent, 100.0 * cpu_count)})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass

    save_process_state(current_state)
    process_cpu.sort(key=lambda x: x['cpu_percent'], reverse=True)
    return process_cpu[:count]


def generate_output():
    """Generate waybar output - optimized for < 20ms execution"""
    start_time = time.time()
    
    history = load_history()
    cpu_history = history.get('cpu', deque(maxlen=TOOLTIP_WIDTH))
    per_core_history = history.get('per_core', {})

    cpu_name = get_cpu_name()
    max_cpu_temp = 0

    # Temperature reading
    try:
        temps = psutil.sensors_temperatures() or {}
        for label in ["k10temp", "coretemp", "zenpower"]:
            if label in temps:
                for t in temps[label]:
                    if t.current > max_cpu_temp:
                        max_cpu_temp = int(t.current)
    except Exception:
        pass

    # Frequency reading
    current_freq = max_freq = 0
    try:
        cpu_info = psutil.cpu_freq(percpu=False)
        if cpu_info:
            current_freq = cpu_info.current or 0
            max_freq = cpu_info.max or cpu_info.max or 0
    except Exception:
        pass

    # Power calculation - prefer zenpower (AMD), fall back to RAPL (Intel)
    cpu_power = 0.0
    zenpower_path = find_zenpower_hwmon()
    if zenpower_path:
        cpu_power = get_zenpower_power(zenpower_path)
    else:
        rapl_path = get_rapl_path()
        if rapl_path:
            cpu_power = calculate_power_nonblocking(rapl_path)

    # Fan speed from nct6687 (all motherboard headers, averaged)
    nct6687_path = find_nct6687_hwmon()
    fan_rpm, fan_percent = get_cpu_fan_speed(nct6687_path)
    fan_profile = read_fan_profile()
    fan_profile_info = FAN_PROFILES[fan_profile]

    # CPU percent (non-blocking)
    cpu_percent, per_core = get_cpu_percent_fast()
    cpu_history.append(cpu_percent)

    # EMA smoothing for per-core
    decay_factor = 0.95
    for i, usage in enumerate(per_core):
        if i not in per_core_history:
            per_core_history[i] = usage
        else:
            per_core_history[i] = (per_core_history[i] * decay_factor) + (usage * (1 - decay_factor))

    # Zombie count
    zombie_count = len(find_zombie_processes())

    # Build tooltip
    tooltip_lines = []
    tooltip_lines.append(
        f"<span foreground='{SECTION_COLORS['CPU']['icon']}'>{CPU_ICON_GENERAL}</span> "
        f"<span foreground='{SECTION_COLORS['CPU']['text']}'>CPU</span> - {cpu_name}"
    )

    # CPU info rows
    freq_percent = (current_freq / max_freq * 100) if max_freq > 0 else 0
    cpu_rows = [
        ("", f"Clock Speed: <span foreground='{get_color(freq_percent, 'cpu_power')}'>{current_freq/1000:.2f} GHz</span> / {max_freq/1000:.2f} GHz"),
        ("\uf2c7", f"Temperature: <span foreground='{get_color(max_cpu_temp, 'cpu_gpu_temp')}'>{max_cpu_temp}°C</span>"),
        ("\uf0e7", f"Power: <span foreground='{get_color(cpu_power, 'cpu_power')}'>{cpu_power:.1f} W</span>"),
        ("󰓅", f"Utilization: <span foreground='{get_color(cpu_percent, 'cpu_power')}'>{cpu_percent:.0f}%</span>"),
        ("󰈐", f"Fan Speed: <span foreground='{get_color(fan_percent, 'cpu_gpu_temp')}'>{fan_rpm} RPM ({fan_percent:.0f}%)</span> [{fan_profile_info['label']}]")
    ]
    
    if zombie_count > 0:
        cpu_rows.append(("󰀨", f"Zombies: <span foreground='{COLORS['red']}'>{zombie_count}</span>"))

    # Calculate line length
    max_line_len = max(len(re.sub(r'<.*?>', '', line_text)) for _, line_text in cpu_rows) + 5
    max_line_len = max(max_line_len, 29)
    tooltip_lines.append(f"<span foreground='{COLORS['bright_black']}'>{'─' * max_line_len}</span>")

    for icon, text_row in cpu_rows:
        tooltip_lines.append(f"{icon} │ {text_row}")

    # CPU visualization box
    cpu_viz_width = 25
    center_padding = " " * int((max_line_len - cpu_viz_width) // 2)
    substrate_color = get_color(max_cpu_temp, 'cpu_gpu_temp')
    border_color = COLORS['white']

    # Unicode box drawing
    tooltip_lines.append("")
    tooltip_lines.append(f"{center_padding}  <span foreground='{border_color}'>\u256d\u2500\u2500\u2518\u2514\u2500\u2500\u2500\u2500\u2518\u283f\u2514\u2500\u2500\u2500\u2500\u2500\u2518\u2514\u2500\u256e</span>")
    tooltip_lines.append(f"{center_padding}  <span foreground='{border_color}'>\u2502</span><span foreground='{substrate_color}'>\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591</span><span foreground='{border_color}'>\u2502</span>")
    tooltip_lines.append(f"{center_padding}  <span foreground='{border_color}'>\u2518</span><span foreground='{substrate_color}'>\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591</span><span foreground='{border_color}'>\u2514</span>")

    # Per-core grid
    num_cores = len(per_core)
    cols = 4
    rows = math.ceil(num_cores / cols)

    for r in range(rows):
        line_parts = [f"{center_padding}  <span foreground='{border_color}'>\u2502</span><span foreground='{substrate_color}'>\u2591\u2591</span>"]
        for c in range(cols):
            idx = r * cols + c
            if idx < num_cores:
                usage = per_core[idx]
                color = get_core_color(usage)
                circle = "\u25cf" if usage >= 10 else "\u25cb"
                line_parts.append(f"<span foreground='{border_color}'>[</span><span foreground='{color}'>{circle}</span><span foreground='{border_color}'>]</span>")
            else:
                line_parts.append(f"<span foreground='{substrate_color}'>\u2591\u2591\u2591</span>")
            if c < cols - 1:
                line_parts.append(f"<span foreground='{substrate_color}'>\u2591</span>")
        line_parts.append(f"<span foreground='{substrate_color}'>\u2591\u2591</span><span foreground='{border_color}'>\u2502</span>")
        tooltip_lines.append("".join(line_parts))

    # Box bottom
    tooltip_lines.append(f"{center_padding}  <span foreground='{border_color}'>\u2510</span><span foreground='{substrate_color}'>\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591</span><span foreground='{border_color}'>\u250c</span>")
    tooltip_lines.append(f"{center_padding}  <span foreground='{border_color}'>\u2502</span><span foreground='{substrate_color}'>\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591\u2591</span><span foreground='{border_color}'>\u2502</span>")
    tooltip_lines.append(f"{center_padding}  <span foreground='{border_color}'>\u2570\u2500\u2500\u2510\u250c\u2500\u2500\u2500\u2500\u2510\u28f6\u250c\u2500\u2500\u2500\u2500\u2500\u2510\u250c\u2500\u256f</span>")

    # Top processes
    tooltip_lines.append("")
    tooltip_lines.append("Top Current Processes:")
    
    top_procs = get_top_processes(3)
    for proc in top_procs:
        name = proc['name']
        usage = proc['cpu_percent']
        if len(name) > 15:
            name = name[:14] + "\u2026"
        color = get_core_color(usage)
        tooltip_lines.append(f" \u2022 {name:<15} <span foreground='{color}'>\uf2db {usage:>5.1f}%</span>")

    # Footer
    tooltip_lines.append("")
    tooltip_lines.append(f"<span foreground='{COLORS['bright_black']}'>{'─' * max_line_len}</span>")
    tooltip_lines.append("󰍽 LMB: Btop │ MMB: Fan Profile │ RMB: Zombies")

    # Save state
    save_history(cpu_history, per_core_history)
    
    # Debug: Log execution time
    exec_time = (time.time() - start_time) * 1000
    if exec_time > 50:  # Log slow executions
        try:
            with open("/tmp/waybar_cpu_debug.log", "a") as f:
                f.write(f"Slow execution: {exec_time:.2f}ms\n")
        except:
            pass

    return {
        "text": f"{CPU_ICON_GENERAL} <span foreground='{get_color(max_cpu_temp, 'cpu_gpu_temp')}'>{max_cpu_temp}\u00b0C</span> {fan_profile_info['icon']}",
        "tooltip": f"<span size='12000'>{'\n'.join(tooltip_lines)}</span>",
        "markup": "pango",
        "class": "cpu"
    }


def main():
    parser = argparse.ArgumentParser(description="Waybar CPU Module")
    parser.add_argument("--kill-zombies", action="store_true",
                       help="Check zombie processes and show notification")
    parser.add_argument("--toggle-fan-profile", action="store_true",
                       help="Toggle fan profile between desktop and gaming")
    args = parser.parse_args()

    if args.toggle_fan_profile:
        toggle_fan_profile()
    elif args.kill_zombies:
        kill_zombie_processes()
    else:
        output = generate_output()
        print(json.dumps(output))


if __name__ == "__main__":
    main()
