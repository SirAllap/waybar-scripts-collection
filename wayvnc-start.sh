#!/bin/bash
# Start wayvnc on a persistent headless output with wl-mirror
# showing the real screen content. Survives monitor off/DPMS.

PRIMARY="DP-3"

# Create headless output
hyprctl output create headless
sleep 0.3
HEADLESS=$(hyprctl monitors -j | jq -r '[.[] | select(.name | startswith("HEADLESS"))] | last | .name')

if [ -z "$HEADLESS" ] || [ "$HEADLESS" = "null" ]; then
    echo "Failed to create headless output" >&2
    exit 1
fi

echo "$HEADLESS" > /tmp/wayvnc-headless-output
echo "VNC output: $HEADLESS, mirroring $PRIMARY via wl-mirror"

# Mirror DP-3 fullscreen onto the headless output
wl-mirror --fullscreen-output "$HEADLESS" "$PRIMARY" &
MIRROR_PID=$!
sleep 0.5

# Start wayvnc capturing the headless output
wayvnc -o "$HEADLESS"
WAYVNC_EXIT=$?

# Cleanup
kill $MIRROR_PID 2>/dev/null
exit $WAYVNC_EXIT
