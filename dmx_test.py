#!/usr/bin/env python3
"""
Standalone DMX tester — run on the Pi to fire a single DMX channel directly,
with no web server involved. Useful for finding which channel/level actually
drives the fog machine (e.g. a Eurolite N-250).

    ~/venv/bin/python3 dmx_test.py                 # channel 1, value 255, 8s
    ~/venv/bin/python3 dmx_test.py -c 1 -v 200     # channel 1 at 200
    ~/venv/bin/python3 dmx_test.py -c 3 -v 255 -d 5
    ~/venv/bin/python3 dmx_test.py --sweep         # try channels 1..16 in turn

Sends raw Open-DMX (BREAK + 250 kbaud + start code + 512 channels), which is
what a dumb FTDI USB-DMX adapter expects. Requires pyserial (in the venv).
"""

import argparse
import time

import serial
import serial.tools.list_ports

DMX_BAUD = 250_000
UNIVERSE = 512


def find_port():
    for p in serial.tools.list_ports.comports():
        if "USB" in (p.description or "").upper() or "ACM" in p.device:
            return p.device
    return "/dev/ttyUSB0"


def frame(channel, value):
    buf = bytearray(UNIVERSE + 1)  # start code + 512 channels
    buf[0] = 0x00
    buf[channel] = value           # channel is 1-based → index = channel
    return bytes(buf)


def blast(ser, channel, value, duration):
    end = time.time() + duration
    f = frame(channel, value)
    while time.time() < end:
        ser.break_condition = True
        time.sleep(0.001)          # BREAK ≥ 88 µs
        ser.break_condition = False
        time.sleep(0.00002)        # mark-after-break
        ser.write(f)
        ser.flush()
        time.sleep(0.023)


def main():
    ap = argparse.ArgumentParser(description="Fire a DMX channel directly for testing.")
    ap.add_argument("-c", "--channel", type=int, default=1, help="DMX channel 1-512 (default 1)")
    ap.add_argument("-v", "--value", type=int, default=255, help="DMX value 0-255 (default 255)")
    ap.add_argument("-d", "--duration", type=float, default=8.0, help="seconds to hold (default 8)")
    ap.add_argument("-p", "--port", default=None, help="serial device (default: auto-detect)")
    ap.add_argument("--sweep", action="store_true",
                    help="cycle channels 1..16 at --value, --duration each, to find the fog channel")
    args = ap.parse_args()

    port = args.port or find_port()
    channel = max(1, min(UNIVERSE, args.channel))
    value = max(0, min(255, args.value))

    try:
        ser = serial.Serial(port, baudrate=DMX_BAUD, stopbits=2, timeout=1)
    except serial.SerialException as exc:
        print(f"Could not open {port}: {exc}")
        print("Is the USB-DMX adapter plugged in, and is this user in the 'dialout' group?")
        return

    with ser:
        if args.sweep:
            print(f"Sweeping channels 1..16 at value {value}, {args.duration}s each, on {port}.")
            print("Watch the fogger — note which channel makes it fire, then use -c that.")
            for ch in range(1, 17):
                print(f"  channel {ch} = {value} …", flush=True)
                blast(ser, ch, value, args.duration)
                blast(ser, ch, 0, 0.2)  # off before next
            print("Sweep done.")
        else:
            print(f"DMX ch{channel} = {value} on {port} for {args.duration}s — watch the fogger…")
            blast(ser, channel, value, args.duration)
            blast(ser, channel, 0, 0.3)  # send off
            print("Done (sent off).")


if __name__ == "__main__":
    main()
