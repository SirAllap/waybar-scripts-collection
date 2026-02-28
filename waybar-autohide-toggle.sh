#!/usr/bin/env bash

BINARY="$HOME/.config/hypr/scripts/waybar_auto_hide"
SIGNAL=9

toggle() {
    if pgrep -f "$BINARY" > /dev/null; then
        pkill -f "$BINARY"
    else
        "$BINARY" &
        disown
    fi
    pkill -x -RTMIN+$SIGNAL waybar
}

status() {
    if pgrep -f "$BINARY" > /dev/null; then
        printf '{"text":"󰘳","class":"enabled","tooltip":"Auto-hide: ON\\nClick to disable"}\n'
    else
        printf '{"text":"󰘳","class":"disabled","tooltip":"Auto-hide: OFF\\nClick to enable"}\n'
    fi
}

case "$1" in
    --toggle) toggle ;;
    *) status ;;
esac
