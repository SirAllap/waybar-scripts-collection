#!/usr/bin/env python3
"""Watch LocalSend receive history, fire desktop notifications for new items.

Clicking the notification (default action) brings LocalSend window to focus.
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import gi
gi.require_version("Notify", "0.7")
from gi.repository import GLib, Notify  # noqa: E402

PREFS = Path.home() / ".local/share/org.localsend.localsend_app/shared_preferences.json"
APP_NAME = "LocalSend"


def load_history_ids():
    try:
        with PREFS.open() as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return set(), []
    raw = data.get("flutter.ls_receive_history", [])
    items = []
    for entry in raw:
        try:
            items.append(json.loads(entry))
        except (json.JSONDecodeError, TypeError):
            continue
    ids = {it.get("id") for it in items if it.get("id")}
    return ids, items


def open_localsend(*_):
    # Toggle the special workspace to show LocalSend
    subprocess.run(
        ["hyprctl", "dispatch", "togglespecialworkspace", "localsend"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def open_file_location(path):
    def handler(*_):
        parent = os.path.dirname(path) or os.path.expanduser("~")
        subprocess.Popen(
            ["xdg-open", parent],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return handler


# Keep notification refs alive so GLib can invoke callbacks
_live_notifications = []


def notify(item):
    sender = item.get("senderAlias", "Unknown")
    name = item.get("fileName", "file")
    is_message = item.get("isMessage", False)
    path = item.get("path")
    ftype = item.get("fileType", "file")

    if is_message:
        summary = f"Message from {sender}"
        body = (name[:200] + "…") if len(name) > 200 else name
        icon = "dialog-information"
    else:
        summary = f"{ftype.capitalize()} from {sender}"
        body = name if not path else f"{name}\n{path}"
        icon = "folder-download"

    n = Notify.Notification.new(summary, body, icon)
    n.set_app_name(APP_NAME)
    n.set_timeout(6000)
    # Default action fires on click
    n.add_action("default", "Open", open_localsend, None)
    if path and not is_message:
        n.add_action("open-folder", "Open folder", open_file_location(path), None)

    def on_closed(notif):
        if notif in _live_notifications:
            _live_notifications.remove(notif)
    n.connect("closed", on_closed)

    _live_notifications.append(n)
    try:
        n.show()
    except GLib.Error:
        pass


def watcher_loop(seen_ids):
    while not PREFS.exists():
        time.sleep(2)
    proc = subprocess.Popen(
        ["inotifywait", "-m", "-e", "modify,close_write", str(PREFS)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    for _ in proc.stdout:
        time.sleep(0.3)
        current_ids, items = load_history_ids()
        new_ids = current_ids - seen_ids
        if new_ids:
            for item in items:
                if item.get("id") in new_ids:
                    GLib.idle_add(notify, item)
            seen_ids = current_ids


def main():
    Notify.init(APP_NAME)
    seen_ids, _ = load_history_ids()
    thread = threading.Thread(target=watcher_loop, args=(seen_ids,), daemon=True)
    thread.start()
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        loop.quit()


if __name__ == "__main__":
    main()
