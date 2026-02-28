#!/bin/bash
# CPU fan control for nct6687 (all 8 channels via CPU temp)
HWMON=/sys/class/hwmon/hwmon7
INTERVAL=5   # seconds between updates
FAN_PROFILE_FILE="/tmp/fan-profile"

# Switch all channels to manual mode
for i in 1 2 3 4 5 6 7 8; do
    echo 1 > "$HWMON/pwm${i}_enable"
done

cleanup() {
    echo "Restoring fans to full speed..."
    for i in 1 2 3 4 5 6 7 8; do
        echo 255 > "$HWMON/pwm${i}"
    done
    exit 0
}
trap cleanup SIGTERM SIGINT

while true; do
    # Read fan profile (default: desktop)
    PROFILE="desktop"
    if [ -r "$FAN_PROFILE_FILE" ]; then
        READ_PROFILE=$(cat "$FAN_PROFILE_FILE" 2>/dev/null)
        case "$READ_PROFILE" in
            desktop|gaming) PROFILE="$READ_PROFILE" ;;
        esac
    fi

    # Set curve parameters based on profile
    if [ "$PROFILE" = "gaming" ]; then
        MINTEMP=35; MAXTEMP=60; MINPWM=120; MAXPWM=255
    else
        MINTEMP=35; MAXTEMP=75; MINPWM=70; MAXPWM=255
    fi

    TEMP=$(( $(cat "$HWMON/temp1_input") / 1000 ))

    if   [ "$TEMP" -le "$MINTEMP" ]; then
        PWM=$MINPWM
    elif [ "$TEMP" -ge "$MAXTEMP" ]; then
        PWM=$MAXPWM
    else
        PWM=$(( MINPWM + (TEMP - MINTEMP) * (MAXPWM - MINPWM) / (MAXTEMP - MINTEMP) ))
    fi

    for i in 1 2 3 4 5 6 7 8; do
        echo "$PWM" > "$HWMON/pwm${i}"
    done

    sleep $INTERVAL
done
