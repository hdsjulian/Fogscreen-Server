#!/usr/bin/env python3
import sys
import RPi.GPIO as GPIO

if len(sys.argv) != 2 or sys.argv[1] not in ('0', '1'):
    print("Usage: toggle_gpio.py <0|1>")
    sys.exit(1)

state = sys.argv[1] == '1'

GPIO.setmode(GPIO.BCM)
GPIO.setup(17, GPIO.OUT)
GPIO.output(17, GPIO.HIGH if state else GPIO.LOW)

print(f"GPIO 17 set to {'HIGH' if state else 'LOW'}")
