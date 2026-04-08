#!/bin/bash
export DISPLAY=:0
until xset q &>/dev/null; do sleep 0.5; done
while true; do
    /usr/bin/feh --fullscreen --auto-zoom --borderless /home/raspi/black.png
done
