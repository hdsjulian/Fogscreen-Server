# Image Upload & Projector Display – Setup Guide

## Overview

`image_upload_server.py` is a lightweight FastAPI server that:

1. Accepts `POST /upload` with a multipart file field named `file`.
2. Returns **200 OK** if the file is a valid image.
3. Returns **415 Unsupported Media Type** if the file is not an image.
4. Displays the image **full-screen** on the connected projector for **30 seconds**.
5. Replaces the image with a **black screen** after 30 seconds.

Nginx proxies external requests on port 80 (`/upload`) to the FastAPI/uvicorn app on port 5000.

---

## Requirements

### System packages (install on the Raspberry Pi)

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv feh nginx
```

> **`feh`** is the image viewer used to display images full-screen.  
> If you prefer another viewer, edit the `display_image_then_black()` function in the script.

### Python packages

```bash
pip3 install fastapi uvicorn pillow pyserial
```

Or using a virtual environment (recommended):

```bash
python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn pillow pyserial
```

---

## Setup Steps

### 1. Copy files to the Raspberry Pi

```bash
scp image_upload_server.py pi@192.168.1.4:~/image_upload_server.py
scp nginx_upload.conf  pi@192.168.1.4:~/nginx_upload.conf
```

### 2. Configure Nginx

```bash
sudo cp ~/nginx_upload.conf /etc/nginx/sites-available/image_upload
sudo ln -s /etc/nginx/sites-available/image_upload /etc/nginx/sites-enabled/image_upload
# Remove the default site if it conflicts on port 80
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

### 3. Allow the server process to access the display

The script sets `DISPLAY=:0` automatically. Make sure the user running the script
is the same user that owns the X session (usually `pi`).

If you run the script as a systemd service (see below), add `Environment=DISPLAY=:0`
to the service file.

### 4. Run the FastAPI server

**Manually (for testing):**

```bash
python3 ~/image_upload_server.py
```

**As a systemd service (recommended for production):**

Create `/etc/systemd/system/image-upload.service`:

```ini
[Unit]
Description=Image Upload & Projector Display Server
After=network.target graphical-session.target

[Service]
User=pi
WorkingDirectory=/home/pi
ExecStart=/usr/bin/python3 /home/pi/image_upload_server.py
Restart=always
Environment=DISPLAY=:0

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable image-upload
sudo systemctl start image-upload
sudo systemctl status image-upload
```

---

## Usage

### Upload an image

```bash
curl -X POST http://192.168.1.4/upload \
     -F "file=@/path/to/photo.jpg"
```

**Success response (200):**
```json
{"message": "Image received and is being displayed."}
```

**Error – not an image (415):**
```json
{"error": "Uploaded file is not a valid image."}
```

**Error – no file attached (400):**
```json
{"error": "No file field in request. Use field name 'file'."}
```

---

## Configuration

Edit the constants at the top of `image_upload_server.py` to customise behaviour:

| Constant          | Default | Description                              |
|-------------------|---------|------------------------------------------|
| `DISPLAY_DURATION`| `30`    | Seconds to show the image before blanking|
| `ALLOWED_MIME_TYPES` | see file | Accepted image MIME types             |
| `ALLOWED_EXTENSIONS` | see file | Accepted file extensions              |

The black "blank" image is generated at 1920×1080. If your projector uses a
different resolution, change the dimensions in `create_black_image()`.

---

## Fog Machine (USB-to-DMX)

The script uses **pyserial** to send DMX512 frames directly over the USB-to-DMX adapter's serial port.

### How it works
- When an image is uploaded, `fog_on()` sends a DMX frame with channel `FOG_DMX_CHANNEL` set to `FOG_ON_VALUE` (255 = full output).
- After `DISPLAY_DURATION` seconds, `fog_off()` sends the same channel at `FOG_OFF_VALUE` (0 = off).
- The adapter is auto-detected as the first USB serial device. You can pin it explicitly by setting `DMX_PORT = "/dev/ttyUSB0"` (or whichever device appears when you plug in the adapter).

### Find your adapter's device path

```bash
ls /dev/ttyUSB* /dev/ttyACM*
# or
dmesg | grep tty | tail -10
```

### Tune the DMX settings

Edit these constants at the top of `image_upload_server.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `DMX_PORT` | `None` (auto) | Serial device, e.g. `"/dev/ttyUSB0"` |
| `FOG_DMX_CHANNEL` | `1` | DMX channel for the fog output |
| `FOG_ON_VALUE` | `255` | Channel value when fog is active (0–255) |
| `FOG_OFF_VALUE` | `0` | Channel value when fog is off |

> **Note:** Some fog machines need a "heat-up" time before they produce fog. If your machine doesn't respond immediately, check its manual – you may need to send a warm-up command on a separate DMX channel first.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `feh: command not found` | `sudo apt install feh` |
| Image doesn't appear on projector | Ensure `DISPLAY=:0` is set and the X server is running |
| Nginx returns 413 | Increase `client_max_body_size` in `nginx_upload.conf` |
| Port 80 already in use | Check `sudo nginx -t` and remove conflicting site configs |
| `[DMX] WARNING: No USB-DMX adapter found` | Check USB connection; set `DMX_PORT` explicitly |
| Fog machine doesn't respond | Verify DMX channel number matches fixture manual |
| `Permission denied` on `/dev/ttyUSB0` | `sudo usermod -aG dialout pi` then log out/in |
