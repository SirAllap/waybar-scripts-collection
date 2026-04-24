#!/usr/bin/env python3
import json
import os
import subprocess
import sys

NOTIFIER = os.path.expanduser("~/.config/waybar/scripts/localsend-notifier.py")


def is_running():
    return subprocess.run(["pgrep", "-x", "localsend"], capture_output=True).returncode == 0


def notifier_running():
    return subprocess.run(["pgrep", "-f", NOTIFIER], capture_output=True).returncode == 0


def start():
    subprocess.Popen(
        ["localsend"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not notifier_running():
        subprocess.Popen(
            [NOTIFIER],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def stop():
    subprocess.run(["pkill", "-x", "localsend"])
    subprocess.run(["pkill", "-f", NOTIFIER])


def toggle():
    if is_running():
        stop()
    else:
        start()


def localsend_window():
    """Return the LocalSend window JSON entry, or None."""
    r = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True)
    try:
        clients = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    for c in clients:
        if c.get("class", "").lower() == "localsend":
            return c
    return None


def current_workspace_id():
    r = subprocess.run(
        ["hyprctl", "activeworkspace", "-j"], capture_output=True, text=True
    )
    try:
        return json.loads(r.stdout).get("id")
    except json.JSONDecodeError:
        return None


def hyprctl(*args):
    subprocess.run(["hyprctl", "dispatch", *args],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def show():
    """Start LocalSend if needed, move its window to current workspace, focus it.
    If it's already on the current workspace and focused, hide it instead."""
    import time
    if not is_running():
        start()
        # Wait for window to appear
        for _ in range(25):
            time.sleep(0.15)
            if localsend_window():
                break

    win = localsend_window()
    if not win:
        return

    target_ws = current_workspace_id()
    if target_ws is None:
        return

    win_ws = win.get("workspace", {}).get("id")
    focused = win.get("focusHistoryID") == 0

    if win_ws == target_ws and focused:
        # Already visible and focused -> hide by sending to special workspace
        subprocess.run(
            ["hyprctl", "dispatch", "movetoworkspacesilent",
             f"special:localsend,address:{win['address']}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        # Bring window to current workspace and focus
        subprocess.run(
            ["hyprctl", "dispatch", "movetoworkspacesilent",
             f"{target_ws},address:{win['address']}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["hyprctl", "dispatch", "focuswindow", f"address:{win['address']}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["hyprctl", "dispatch", "centerwindow"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


if "--toggle" in sys.argv:
    toggle()
elif "--show" in sys.argv:
    show()
else:
    running = is_running()
    print(json.dumps({
        "text": "󰜽",
        "tooltip": "LocalSend: ON — receiving" if running else "LocalSend: OFF",
        "class": "on" if running else "off",
    }))
