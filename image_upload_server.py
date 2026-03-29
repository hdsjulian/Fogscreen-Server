#!/usr/bin/env python3
"""
Image upload server for Raspberry Pi Zero 2 W – built with FastAPI.

POST /upload  – multipart/form-data, field name: "file"
  200  → valid image  (displayed on projector for 30 s, then black screen)
  400  → missing / empty file field
  415  → file is not a valid image

While the image is displayed, a fog machine connected via USB-to-DMX is
triggered at full output and then switched off when the display clears.
Requires: pip install fastapi uvicorn pillow pyserial
"""

import fcntl
import io
import os
import serial
import serial.tools.list_ports
import subprocess
import tempfile
import threading
import time
from pathlib import Path

TIOCSBRK = 0x2000747B  # macOS
TIOCCBRK = 0x2000747A  # macOS

import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image

try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(17, GPIO.OUT, initial=GPIO.LOW)
    GPIO_AVAILABLE = True
    print("GPIO initialized OK", flush=True)
except Exception as e:
    GPIO_AVAILABLE = False
    print(f"GPIO NOT available: {e}", flush=True)

# ── Configuration ─────────────────────────────────────────────────────────────
HOST = "0.0.0.0"
PORT = 5000
DISPLAY_DURATION = 30          # seconds to show the image before going black
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
FOG_DMX_CHANNEL = 248                # DMX channel that controls the fog output
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
relay_state = False  # False = off, True = on


# ── Helpers ───────────────────────────────────────────────────────────────────

def create_black_image() -> None:
    """Generate a 1920×1080 black PNG used to blank the projector."""
    img = Image.new("RGB", (1920, 1080), (0, 0, 0))
    img.save(BLACK_IMAGE_PATH)


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


def _send_dmx(value: int, duration: float = 0.5) -> None:
    """Send continuous DMX frames for `duration` seconds."""
    port = DMX_PORT or _find_dmx_port()
    if port is None:
        print("[DMX] WARNING: No USB-DMX adapter found. Skipping fog control.", flush=True)
        return
    try:
        with serial.Serial(port, baudrate=DMX_BAUD, stopbits=2, timeout=1) as ser:
            end = time.time() + duration
            while time.time() < end:
                fcntl.ioctl(ser.fd, TIOCSBRK)
                time.sleep(0.001)
                fcntl.ioctl(ser.fd, TIOCCBRK)
                time.sleep(0.00002)
                ser.write(_build_dmx_frame(FOG_DMX_CHANNEL, value))
                ser.flush()
                time.sleep(0.023)
    except serial.SerialException as exc:
        print(f"[DMX] Serial error: {exc}", flush=True)


def fog_on() -> None:
    """Trigger the fog machine (non-blocking)."""
    threading.Thread(target=_send_dmx, args=(FOG_ON_VALUE,), daemon=True).start()


def fog_off() -> None:
    """Stop the fog machine (non-blocking)."""
    threading.Thread(target=_send_dmx, args=(FOG_OFF_VALUE,), daemon=True).start()


# ── Main display + fog sequence ───────────────────────────────────────────────

def display_image_then_black(image_path: str) -> None:
    """
    1. Kill any existing display.
    2. Show *image_path* full-screen AND trigger the fog machine.
    3. After DISPLAY_DURATION seconds, stop the fog and show a black screen.
    """
    global current_proc

    with display_lock:
        if current_proc and current_proc.poll() is None:
            current_proc.terminate()
            current_proc.wait()
        current_proc = _show(image_path)

    fog_on()

    time.sleep(DISPLAY_DURATION)

    fog_off()

    with display_lock:
        if current_proc and current_proc.poll() is None:
            current_proc.terminate()
            current_proc.wait()
        current_proc = _show(BLACK_IMAGE_PATH)


# ── FastAPI endpoint ──────────────────────────────────────────────────────────

@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    """
    Accept an image file upload, validate it, display it on the projector,
    and trigger the fog machine for the duration of the display.
    """
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

    # Save temporarily
    ext = Path(file.filename).suffix.lower() or ".png"
    tmp_path = os.path.join(UPLOAD_DIR, f"current_image{ext}")
    with open(tmp_path, "wb") as f:
        f.write(data)

    # Display + fog in background (non-blocking – response returned immediately)
    threading.Thread(
        target=display_image_then_black,
        args=(tmp_path,),
        daemon=True,
    ).start()

    return JSONResponse(
        status_code=200,
        content={"message": "Image received, display and fog machine activated."},
    )


fog_state = False  # False = off, True = on


@app.post("/fog/toggle")
async def fog_toggle():
    """Toggle the fog machine on or off."""
    global fog_state
    fog_state = not fog_state
    if fog_state:
        fog_on()
        print("Fog ON", flush=True)
    else:
        fog_off()
        print("Fog OFF", flush=True)
    return JSONResponse(content={"fog": "on" if fog_state else "off"})


@app.get("/fog/status")
async def fog_status():
    return JSONResponse(content={"fog": "on" if fog_state else "off"})


@app.get("/relay/status")
async def relay_status():
    """Return the current relay state."""
    return JSONResponse(content={"relay": "on" if relay_state else "off"})


@app.post("/relay/toggle")
async def relay_toggle():
    """Toggle the relay on GPIO 17."""
    global relay_state
    relay_state = not relay_state
    if GPIO_AVAILABLE:
        GPIO.output(17, GPIO.LOW if relay_state else GPIO.HIGH)
        print(f"GPIO 17 set to {'LOW (on)' if relay_state else 'HIGH (off)'}", flush=True)
    else:
        print("GPIO not available, skipping relay", flush=True)
    return JSONResponse(content={"relay": "on" if relay_state else "off"})


if __name__ == "__main__":
    create_black_image()
    uvicorn.run(app, host=HOST, port=PORT)
