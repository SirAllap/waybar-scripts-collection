#!/usr/bin/env python3
import json
import subprocess
import sys

STATE_FILE = "/tmp/waybar_wayvnc_state.json"
ICON_IDLE = "󰕑"
ICON_CONNECTED = "󰊓"
ICON_FAILED = "󰕐"


def get_wayvnc_state():
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "wayvnc"],
        capture_output=True, text=True
    )
    return result.stdout.strip()  # "active", "failed", "inactive", etc.


def get_clients():
    try:
        result = subprocess.run(
            ["wayvncctl", "-j", "client-list"],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return []


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"client_ids": []}


def save_state(client_ids):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"client_ids": list(client_ids)}, f)
    except Exception:
        pass


def notify(title, message, urgency="normal"):
    subprocess.run(
        ["notify-send", "-u", urgency, "-t", "6000", title, message],
        capture_output=True
    )


def client_display(client):
    host = client.get("hostname", client.get("address", "unknown"))
    username = client.get("username", "")
    return f"{username}@{host}" if username else host


def main():
    state = get_wayvnc_state()
    if state != "active":
        tooltip = "VNC server failed\nClick to restart" if state == "failed" else "VNC server not running\nClick to start"
        print(json.dumps({
            "text": ICON_FAILED,
            "class": "failed",
            "tooltip": tooltip
        }))
        save_state([])
        return

    clients = get_clients()
    prev_state = load_state()
    prev_ids = set(prev_state.get("client_ids", []))
    current_ids = set(c.get("id") for c in clients)

    for client in clients:
        if client.get("id") not in prev_ids:
            notify("󰊓 VNC Connected", f"{client_display(client)} connected to your desktop")

    for prev_id in prev_ids:
        if prev_id not in current_ids:
            notify("󰕑 VNC Disconnected", "A client disconnected from your desktop", "low")

    save_state(current_ids)

    if clients:
        count = len(clients)
        first = client_display(clients[0])
        text = f"{ICON_CONNECTED} {first}" if count == 1 else f"{ICON_CONNECTED} {count} clients"

        tooltip_lines = [f"<b>VNC — {count} client{'s' if count > 1 else ''} connected</b>"]
        for c in clients:
            tooltip_lines.append(f"  󰊓  {client_display(c)}")
        tooltip_lines.append("")
        tooltip_lines.append("MMB: disconnect all  |  RMB: stop server")

        print(json.dumps({
            "text": text,
            "tooltip": "\n".join(tooltip_lines),
            "class": "connected",
            "markup": "pango"
        }))
    else:
        print(json.dumps({
            "text": ICON_IDLE,
            "tooltip": "VNC server running\nNo clients connected\nMMB: disconnect all  |  RMB: stop server",
            "class": "idle"
        }))


def disconnect_all():
    for client in get_clients():
        cid = client.get("id")
        if cid is not None:
            subprocess.run(["wayvncctl", "client-disconnect", str(cid)], capture_output=True)


if __name__ == "__main__":
    if "--disconnect-all" in sys.argv:
        disconnect_all()
    else:
        main()
