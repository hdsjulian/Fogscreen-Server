#!/usr/bin/env python3
import sys
import subprocess
import time

if len(sys.argv) != 2:
    print("Usage: display_image.py <image_path>")
    sys.exit(1)

image_path = sys.argv[1]
DISPLAY_DURATION = 30

proc = subprocess.Popen(
    ["feh", "--fullscreen", "--auto-zoom", "--borderless", image_path],
    env={**__import__('os').environ, "DISPLAY": ":0"},
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

print(f"Displaying {image_path} for {DISPLAY_DURATION}s...")
time.sleep(DISPLAY_DURATION)

proc.terminate()
proc.wait()
print("Done.")
