#!/bin/bash

: """
Processes cava output from a FIFO pipe using native Pipewire input.
Converts numerical values to Unicode bar characters for Waybar.
"""

pipe="$HOME/.cache/cava.fifo"
bar="  ▂▃▄▅▆▇█"

if [[ ! -p $pipe ]]; then
    mkfifo "$pipe"
fi

pkill -f "cava -p $HOME/.config/cava/waybar.conf"
cava -p "$HOME/.config/cava/waybar.conf" > "$pipe" &

while read -r line; do
    IFS=';' read -ra values <<< "$line"
    output=""
    
    for val in "${values[@]}"; do
        if [[ -n $val ]]; then
            index=$(( (val * 7) / 1000 ))
            output+="${bar:$index:1}"
        fi
    done
    
    echo "$output"
done < "$pipe"
