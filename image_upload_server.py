#!/usr/bin/env python3
"""
Image upload server for Raspberry Pi Zero 2 W – built with FastAPI.

POST /upload  – multipart/form-data, field name: "file"
                optional field "device_time": the uploading device's clock,
                ISO-8601 (e.g. 2026-07-05T22:45:08+02:00), recorded in the log
  200  → valid image. Fans + fog start immediately, building the fog wall
         for `heatup_seconds` before the image is actually projected, which
         is then shown for `dissolve_seconds` before going black. Both
         durations are reported in the JSON body so the client's own
         on-phone ritual (a "warming up" wait, then the fade) can track
         what's actually happening at the installation.
  400  → missing / empty file field
  409  → a picture is already on the fog screen (body includes retry_after)
  415  → file is not a valid image

GET/POST /display/settings – read or tune heatup_seconds / dissolve_seconds
at runtime (also exposed as sliders in the captive-portal admin page),
without a redeploy.

Accepted uploads are logged to ~/fogscreen_uploads.jsonl (filename, size,
device time, server time). The image file itself is deleted after display.

While the image is displayed, a fog machine connected via USB-to-DMX is
triggered at full output and then switched off when the display clears.
Requires: pip install fastapi uvicorn pillow pyserial rpi-hardware-pwm
"""

import io
import json
import os
import serial
import serial.tools.list_ports
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image

# ── Fan (hardware PWM) configuration ──────────────────────────────────────────
# Two hardware-PWM channels (not bit-banged software PWM):
#   channel 0 → GPIO 18 (physical pin 12) → fan array 1
#   channel 1 → GPIO 19 (physical pin 35) → fan array 2
# Requires `dtoverlay=pwm-2chan` in /boot/firmware/config.txt (set by setup.sh).
FAN1_PWM_CHANNEL = 0
FAN2_PWM_CHANNEL = 1
FAN_PWM_FREQ = 25_000  # 25 kHz – Intel 4-pin PWM spec (21–28 kHz acceptable range)
# Fan behaviour while an uploaded image is on the fog: the arrays run so the
# fog wall actually forms a surface to project onto (an upload triggers fog
# *and* fans — not fog alone). After the image clears they cool down, then
# return to whatever speed the portal had them at (0 by default).
FAN_DISPLAY_SPEED = 100   # duty cycle (0–100 %) both arrays run at during a showing
FAN_COOLDOWN_SECS = 5     # keep fans running this long after the image clears
# ──────────────────────────────────────────────────────────────────────────────

fan1_pwm = None  # HardwarePWM objects, set during init below
fan2_pwm = None

try:
    from rpi_hardware_pwm import HardwarePWM
    # chip=0 covers the Pi Zero 2 W (and Pi 0–4). On a Pi 5 this would be chip=2.
    fan1_pwm = HardwarePWM(pwm_channel=FAN1_PWM_CHANNEL, hz=FAN_PWM_FREQ, chip=0)
    fan2_pwm = HardwarePWM(pwm_channel=FAN2_PWM_CHANNEL, hz=FAN_PWM_FREQ, chip=0)
    fan1_pwm.start(0)  # duty cycle 0–100 %
    fan2_pwm.start(0)
    FAN_PWM_AVAILABLE = True
    print("Hardware PWM initialized OK", flush=True)
except Exception as e:
    FAN_PWM_AVAILABLE = False
    print(f"Hardware PWM NOT available: {e}", flush=True)

# ── Configuration ─────────────────────────────────────────────────────────────
HOST = "0.0.0.0"
PORT = 5000
# Mutable at runtime via /display/settings (and the admin page's sliders) —
# same pattern as fog_level / fan{1,2}_speed below. Defaults only; an
# installation operator tunes these on-site without a redeploy.
HEATUP_SECONDS_DEFAULT = 5     # fans + fog run this long before the image is projected
DISSOLVE_SECONDS_DEFAULT = 30  # how long the image is shown before going black
DISPLAY_ENV = ":0"             # X display (usually :0 on the Pi desktop)
UPLOAD_DIR = tempfile.mkdtemp()
BLACK_IMAGE_PATH = os.path.join(UPLOAD_DIR, "_black.png")

ALLOWED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif",
    ".bmp", ".webp", ".tiff", ".tif",
}

# ── DMX / Fog machine configuration ───────────────────────────────────────────
# USB-to-DMX adapters (e.g. Enttec Open DMX, DMXKing) appear as a serial port.
# Set DMX_PORT to the device path, or leave as None to auto-detect the first
# USB serial device found.
DMX_PORT: str | None = None          # e.g. "/dev/ttyUSB0" or "/dev/ttyACM0"
DMX_BAUD = 250_000                   # DMX512 baud rate (do not change)

# DMX channel layout for your fog machine.
# Adjust channel numbers (1-based) and values to match your fixture's manual.
FOG_DMX_CHANNEL = 1                  # DMX channel that controls the fog output
FOG_ON_VALUE    = 255                # 0-255 – full fog
FOG_OFF_VALUE   = 0                  # 0     – fog off

DMX_UNIVERSE_SIZE = 512              # standard DMX universe
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

display_lock = threading.Lock()
current_proc: subprocess.Popen | None = None
display_slot = threading.Lock()  # held for the whole display sequence — blocks new uploads
display_until = 0.0              # epoch seconds when the current display ends
UPLOAD_LOG_PATH = Path.home() / "fogscreen_uploads.jsonl"
fan1_speed  = 0      # array 1 duty cycle 0–100 %
fan2_speed  = 0      # array 2 duty cycle 0–100 %
fog_level   = 70     # fog output percentage 0–100 (maps to DMX 0–255)
heatup_seconds   = HEATUP_SECONDS_DEFAULT    # fog-wall build time before the image shows
dissolve_seconds = DISSOLVE_SECONDS_DEFAULT  # how long the image is shown


# ── Helpers ───────────────────────────────────────────────────────────────────

def create_black_image() -> None:
    """Generate a 1920×1080 black PNG used to blank the projector."""
    img = Image.new("RGB", (1920, 1080), (0, 0, 0))
    img.save(BLACK_IMAGE_PATH)


def log_upload(filename: str, size_bytes: int, device_time: str | None) -> None:
    """Append an upload record to the JSONL log.

    device_time is the phone's clock (ISO-8601, sent by the app) — treat it as
    authoritative: the Pi has no RTC and no NTP while in AP mode, so
    server_time is only a plausibility cross-check.
    """
    entry = {
        "filename": filename,
        "size_bytes": size_bytes,
        "device_time": device_time,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        with open(UPLOAD_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        print(f"[Log] Could not write upload log: {exc}", flush=True)


def is_valid_image(data: bytes, filename: str) -> bool:
    """Return True only if *data* is a real image Pillow can decode."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        return True
    except Exception:
        return False


def _show(path: str) -> subprocess.Popen:
    """Launch feh full-screen and return the process handle."""
    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY_ENV
    return subprocess.Popen(
        ["feh", "--fullscreen", "--auto-zoom", "--borderless", path],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ── DMX helpers ───────────────────────────────────────────────────────────────

def _find_dmx_port() -> str | None:
    """Auto-detect the first USB serial port (likely the DMX adapter)."""
    for port in serial.tools.list_ports.comports():
        if "USB" in (port.description or "").upper() or "ACM" in port.device:
            return port.device
    return None


def _build_dmx_frame(channel: int, value: int) -> bytes:
    """
    Build a minimal DMX512 frame.
    DMX512 over serial: BREAK (low for ≥88 µs) + MAB + start code (0x00) + 512 channel bytes.
    Most USB-DMX adapters handle the BREAK/MAB in hardware; we just send the
    start code followed by the channel data.
    """
    frame = bytearray(DMX_UNIVERSE_SIZE + 1)  # start code + 512 channels
    frame[0] = 0x00                            # DMX start code
    frame[channel] = value                     # channel is 1-based → index = channel
    return bytes(frame)


def _dmx_break(ser) -> None:
    """
    Send the DMX512 BREAK + mark-after-break before a frame, using pyserial's
    portable break control. (The old code used raw TIOCSBRK/TIOCCBRK ioctls with
    macOS-only constants, which errored on the Pi and killed the DMX thread
    before a single frame went out — so the fog never fired.)
    """
    ser.break_condition = True
    time.sleep(0.001)      # BREAK — spec wants ≥ 88 µs; 1 ms is safely long
    ser.break_condition = False
    time.sleep(0.00002)    # mark-after-break


def _send_dmx(value: int, duration: float = 0.5, channel: int = FOG_DMX_CHANNEL) -> None:
    """Send continuous DMX frames (channel=value) for `duration` seconds."""
    port = DMX_PORT or _find_dmx_port()
    if port is None:
        print("[DMX] WARNING: No USB-DMX adapter found. Skipping fog control.", flush=True)
        return
    try:
        with serial.Serial(port, baudrate=DMX_BAUD, stopbits=2, timeout=1) as ser:
            end = time.time() + duration
            while time.time() < end:
                _dmx_break(ser)
                ser.write(_build_dmx_frame(channel, value))
                ser.flush()
                time.sleep(0.023)
    except serial.SerialException as exc:
        print(f"[DMX] Serial error: {exc}", flush=True)


def fog_on(duration: float) -> None:
    """Trigger the fog machine for `duration` seconds (non-blocking)."""
    dmx_value = round(fog_level / 100 * 255)
    threading.Thread(target=_send_dmx, args=(dmx_value, duration), daemon=True).start()


def fog_off() -> None:
    """Stop the fog machine (non-blocking)."""
    threading.Thread(target=_send_dmx, args=(FOG_OFF_VALUE, 1.0), daemon=True).start()


fog_stop_event = threading.Event()


def _fog_sequence(duration: int, level_pct: int) -> None:
    """
    Stream DMX at level_pct% for duration seconds, run fans for duration+5s, then stop.
    Responds to fog_stop_event for early cancellation.
    """
    global fog_state
    fog_stop_event.clear()
    dmx_value = round(level_pct / 100 * 255)

    # Start both fan arrays at their configured speeds
    if FAN_PWM_AVAILABLE:
        if fan1_pwm is not None: fan1_pwm.change_duty_cycle(fan1_speed)
        if fan2_pwm is not None: fan2_pwm.change_duty_cycle(fan2_speed)
        print(f"[Fan] ON at {fan1_speed}% / {fan2_speed}%", flush=True)

    # Stream DMX for duration seconds (or until stopped early)
    port = DMX_PORT or _find_dmx_port()
    if port is None:
        print("[DMX] No adapter found – skipping fog", flush=True)
    else:
        try:
            with serial.Serial(port, baudrate=DMX_BAUD, stopbits=2, timeout=1) as ser:
                end = time.time() + duration
                while time.time() < end and not fog_stop_event.is_set():
                    _dmx_break(ser)
                    ser.write(_build_dmx_frame(FOG_DMX_CHANNEL, dmx_value))
                    ser.flush()
                    time.sleep(0.023)
                # Always send an off frame when done
                ser.write(_build_dmx_frame(FOG_DMX_CHANNEL, FOG_OFF_VALUE))
                ser.flush()
        except serial.SerialException as exc:
            print(f"[DMX] Serial error: {exc}", flush=True)

    # Fan cool-down: 5 more seconds (skipped on early stop)
    if not fog_stop_event.is_set():
        cool_end = time.time() + 5
        while time.time() < cool_end and not fog_stop_event.is_set():
            time.sleep(0.05)

    # Stop fans
    if FAN_PWM_AVAILABLE:
        if fan1_pwm is not None: fan1_pwm.change_duty_cycle(0)
        if fan2_pwm is not None: fan2_pwm.change_duty_cycle(0)
        print("[Fan] OFF", flush=True)

    fog_state = False
    print("[Fog] Sequence complete", flush=True)


# ── Fan helpers for the upload/display flow ───────────────────────────────────

def _fans_run(pct: int) -> None:
    """Run both fan arrays at pct% (does not change the portal-set fan speeds)."""
    if not FAN_PWM_AVAILABLE:
        print(f"[Fan] PWM not available – would run both arrays at {pct}%", flush=True)
        return
    if fan1_pwm is not None: fan1_pwm.change_duty_cycle(pct)
    if fan2_pwm is not None: fan2_pwm.change_duty_cycle(pct)
    print(f"[Fan] Both arrays at {pct}% for image display", flush=True)


def _fans_restore() -> None:
    """Return both arrays to their configured (portal-set) speeds — 0 by default."""
    if not FAN_PWM_AVAILABLE:
        return
    if fan1_pwm is not None: fan1_pwm.change_duty_cycle(fan1_speed)
    if fan2_pwm is not None: fan2_pwm.change_duty_cycle(fan2_speed)
    print(f"[Fan] Restored to {fan1_speed}% / {fan2_speed}%", flush=True)


# ── Main display + fog sequence ───────────────────────────────────────────────

def display_image_then_black(image_path: str, heatup: int, dissolve: int) -> None:
    """
    1. Kill any existing display, show black while the fog wall builds.
    2. Start fans + fog immediately — the wall needs `heatup` seconds to
       form a surface before there's anything to project onto.
    3. Show *image_path* full-screen for `dissolve` seconds.
    4. Stop the fog, show a black screen, then let the fans cool down and
       return to their configured speed.
    """
    global current_proc

    with display_lock:
        if current_proc and current_proc.poll() is None:
            current_proc.terminate()
            current_proc.wait()
        current_proc = _show(BLACK_IMAGE_PATH)

    _fans_run(FAN_DISPLAY_SPEED)
    fog_on(float(heatup + dissolve))

    if heatup > 0:
        time.sleep(heatup)

    with display_lock:
        if current_proc and current_proc.poll() is None:
            current_proc.terminate()
            current_proc.wait()
        current_proc = _show(image_path)

    time.sleep(dissolve)

    fog_off()

    with display_lock:
        if current_proc and current_proc.poll() is None:
            current_proc.terminate()
            current_proc.wait()
        current_proc = _show(BLACK_IMAGE_PATH)

    # Let the fog clear against moving air, then hand the fans back to whatever
    # speed the portal had them at (0 = off, the default).
    time.sleep(FAN_COOLDOWN_SECS)
    _fans_restore()


def run_display_then_cleanup(image_path: str, heatup: int, dissolve: int) -> None:
    """Display the image, then erase it from disk (oblivion) and free the slot."""
    try:
        display_image_then_black(image_path, heatup, dissolve)
    finally:
        try:
            os.remove(image_path)
        except OSError:
            pass
        display_slot.release()


# ── FastAPI endpoint ──────────────────────────────────────────────────────────

@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    device_time: str | None = Form(None),
):
    """
    Accept an image file upload, validate it, display it on the projector,
    and trigger the fog machine for the duration of the display.
    Only one picture at a time: returns 409 while a display is in progress.
    """
    global display_until

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file selected.")

    data = await file.read()

    if not data:
        raise HTTPException(status_code=400, detail="Empty file.")

    if not is_valid_image(data, file.filename):
        return JSONResponse(
            status_code=415,
            content={"error": "Uploaded file is not a valid image."},
        )

    # display_slot is released by run_display_then_cleanup once the sequence ends
    if not display_slot.acquire(blocking=False):
        retry_after = max(1, round(display_until - time.time()))
        return JSONResponse(
            status_code=409,
            content={
                "error": "A picture is currently on the fog screen.",
                "retry_after": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )

    try:
        # Snapshot the current settings so a mid-sequence /display/settings
        # change can't desync this upload's response from what actually runs.
        heatup, dissolve = heatup_seconds, dissolve_seconds

        # Slot stays held through the fan cool-down too, so reflect that in the
        # retry-after the busy (409) response reports.
        display_until = time.time() + heatup + dissolve + FAN_COOLDOWN_SECS

        # Save temporarily — deleted again after the display sequence
        ext = Path(file.filename).suffix.lower() or ".png"
        tmp_path = os.path.join(UPLOAD_DIR, f"current_image{ext}")
        with open(tmp_path, "wb") as f:
            f.write(data)

        log_upload(file.filename, len(data), device_time)

        # Display + fog in background (non-blocking – response returned immediately)
        threading.Thread(
            target=run_display_then_cleanup,
            args=(tmp_path, heatup, dissolve),
            daemon=True,
        ).start()
    except Exception:
        display_slot.release()
        raise

    return JSONResponse(
        status_code=200,
        content={
            "message": "Image received, display and fog machine activated.",
            "heatup_seconds": heatup,
            "dissolve_seconds": dissolve,
        },
    )


fog_state = False  # False = off, True = on


@app.post("/fog/toggle")
async def fog_toggle(duration: int = 30, level: int = 70):
    """Start a timed fog+fan sequence, or cancel one in progress."""
    global fog_state, fog_level
    if fog_state:
        fog_stop_event.set()
        fog_state = False
        print("[Fog] Stopped early", flush=True)
    else:
        fog_state = True
        fog_level = level
        threading.Thread(target=_fog_sequence, args=(duration, level), daemon=True).start()
        print(f"[Fog] Sequence started: {duration}s at {level}%", flush=True)
    return JSONResponse(content={"fog": "on" if fog_state else "off", "fog_level": fog_level})


@app.get("/fog/status")
async def fog_status():
    return JSONResponse(content={"fog": "on" if fog_state else "off", "fog_level": fog_level})


# ── DMX test (find the right channel/level for an unknown fixture) ─────────────

@app.post("/dmx/test")
async def dmx_test(channel: int = 1, value: int = 255, duration: float = 5.0):
    """Hold a single DMX channel at a value for a few seconds — used from the
    web panel's channel dropdown + output slider to discover what drives the
    fog machine. value is 0–255 (raw DMX), not a percentage."""
    channel = max(1, min(DMX_UNIVERSE_SIZE, channel))
    value = max(0, min(255, value))
    duration = max(0.1, min(60.0, duration))
    threading.Thread(target=_send_dmx, args=(value, duration, channel), daemon=True).start()
    print(f"[DMX] test: channel {channel} = {value} for {duration}s", flush=True)
    return JSONResponse(content={"channel": channel, "value": value, "duration": duration})


# ── Fan speed control ────────────────────────────────────────────────────────

def _set_fan_speed(array: int, speed_pct: int) -> None:
    """Set PWM duty cycle for fan array 1 or 2 (0–100 %)."""
    global fan1_speed, fan2_speed
    speed_pct = max(0, min(100, speed_pct))
    if array == 1:
        fan1_speed = speed_pct
        pwm = fan1_pwm
    else:
        fan2_speed = speed_pct
        pwm = fan2_pwm
    if FAN_PWM_AVAILABLE and pwm is not None:
        pwm.change_duty_cycle(speed_pct)
        print(f"[Fan] Array {array} speed set to {speed_pct}%", flush=True)
    else:
        print(f"[Fan] PWM not available – would set array {array} to {speed_pct}%", flush=True)


@app.post("/fan/1/speed")
async def set_fan1_speed(speed: int = 0):
    _set_fan_speed(1, speed)
    return JSONResponse(content={"fan1_speed": fan1_speed})


@app.post("/fan/2/speed")
async def set_fan2_speed(speed: int = 0):
    _set_fan_speed(2, speed)
    return JSONResponse(content={"fan2_speed": fan2_speed})


@app.get("/fan/status")
async def fan_status():
    return JSONResponse(content={"fan1_speed": fan1_speed, "fan2_speed": fan2_speed})


# ── Display timing (heat-up + dissolve) ───────────────────────────────────────

@app.get("/display/settings")
async def get_display_settings():
    return JSONResponse(content={"heatup_seconds": heatup_seconds, "dissolve_seconds": dissolve_seconds})


@app.post("/display/settings")
async def set_display_settings(heatup: int | None = None, dissolve: int | None = None):
    """Tune the per-upload timing at runtime — no redeploy needed on-site."""
    global heatup_seconds, dissolve_seconds
    if heatup is not None:
        heatup_seconds = max(0, min(60, heatup))
    if dissolve is not None:
        dissolve_seconds = max(5, min(120, dissolve))
    return JSONResponse(content={"heatup_seconds": heatup_seconds, "dissolve_seconds": dissolve_seconds})


# ── Captive portal detection endpoints ───────────────────────────────────────
# Return 204 so iOS/Android mark the network as connected with no popup.

@app.get("/hotspot-detect.html")
@app.get("/library/test/success.html")
@app.get("/ncsi.txt")
@app.get("/generate_204")
async def captive_portal_bypass():
    return PlainTextResponse("", status_code=204)


if __name__ == "__main__":
    create_black_image()
    uvicorn.run(app, host=HOST, port=PORT)
