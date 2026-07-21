#!/usr/bin/env python3
"""
End-to-end tester for the Temple / Fogscreen server — run from your laptop.

    python3 test_fogscreen.py                     # defaults to http://192.168.4.1:5000
    python3 test_fogscreen.py --host 192.168.1.50 # if the Pi is in client mode
    python3 test_fogscreen.py --no-prompts        # HTTP checks only, no y/n asks

First join your Mac to the Temple's Wi-Fi ("The Temple of Digital Oblivion"),
or point --host at the Pi's address if it's on your network in client mode.

It exercises every server endpoint (reachability, captive portal, fans, fog,
upload happy-path, and the 409/415/400 rejection paths) and — because they
can't be seen over the network — prompts you to confirm the fans spin, the fog
fires, and the image appears on the projector. Stdlib only; no dependencies.
"""

import argparse
import json
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone

# ── tiny terminal helpers ─────────────────────────────────────────────────────
GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
results = []  # (name, ok_or_None)  None = needs human confirmation / skipped


def record(name, ok, detail=""):
    tag = f"{GREEN}PASS{RESET}" if ok else (f"{YELLOW}????{RESET}" if ok is None else f"{RED}FAIL{RESET}")
    print(f"  [{tag}] {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
    results.append((name, ok))


def section(title):
    print(f"\n{title}")


# ── HTTP ──────────────────────────────────────────────────────────────────────
def http(method, url, data=None, headers=None, timeout=12):
    """Return (status_code_or_None, body_text). Never raises for HTTP errors."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def multipart_body(fields, file_part):
    """fields: dict[str,str]; file_part: (field, filename, bytes, content_type) or None."""
    boundary = "----temple-test-boundary-7f3a9c"
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += value.encode() + b"\r\n"
    if file_part:
        name, filename, content, ctype = file_part
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        out += f"Content-Type: {ctype}\r\n\r\n".encode()
        out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def make_png(w, h, rgb):
    """Build a solid-colour PNG in pure stdlib (visible on the projector)."""
    def chunk(typ, data):
        body = typ + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit, colour type 2 (RGB)
    row = b"\x00" + bytes(rgb) * w                        # filter byte 0 + pixels
    raw = row * h
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def device_time():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ── the tests ─────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Test the Temple / Fogscreen server.")
    ap.add_argument("--host", default="192.168.4.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--no-prompts", action="store_true", help="skip the confirm-the-physical-effect questions")
    args = ap.parse_args()
    base = f"http://{args.host}:{args.port}"

    def confirm(prompt):
        if args.no_prompts:
            record(prompt, None, "not confirmed (--no-prompts)")
            return
        try:
            ans = input(f"  {YELLOW}>>{RESET} {prompt} [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        record(prompt, True if ans == "y" else (False if ans == "n" else None))

    print(f"Testing Temple server at {base}")
    print("Make sure your Mac is on the Temple Wi-Fi (or the Pi's network).")

    # 1. Reachability -----------------------------------------------------------
    section("1. Reachability")
    code, body = http("GET", f"{base}/fan/status")
    if code is None:
        record("server reachable", False, body)
        print(f"\n{RED}Can't reach the server.{RESET} Are you on the Temple Wi-Fi, "
              f"and is the Pi in AP mode with fogscreen running? Aborting.")
        summary()
        return
    record("server reachable (/fan/status 200)", code == 200, f"HTTP {code}")

    # 2. Captive portal ---------------------------------------------------------
    section("2. Captive portal")
    code, _ = http("GET", f"{base}/generate_204")
    record("captive-portal /generate_204 → 204", code == 204, f"HTTP {code}")

    # 3. Fans -------------------------------------------------------------------
    section("3. Fans (hardware PWM)")
    http("POST", f"{base}/fan/1/speed?speed=70")
    http("POST", f"{base}/fan/2/speed?speed=70")
    code, body = http("GET", f"{base}/fan/status")
    try:
        st = json.loads(body)
        ok = st.get("fan1_speed") == 70 and st.get("fan2_speed") == 70
    except Exception:
        ok = False
    record("set both fans to 70% (/fan/status reflects it)", ok, body)
    confirm("Are BOTH fan arrays now spinning?")
    http("POST", f"{base}/fan/1/speed?speed=0")
    http("POST", f"{base}/fan/2/speed?speed=0")
    record("fans reset to 0%", True)

    # 4. Fog (DMX) --------------------------------------------------------------
    section("4. Fog machine (USB-DMX)")
    code, body = http("POST", f"{base}/fog/toggle?duration=6&level=70")
    on = False
    try:
        on = json.loads(body).get("fog") == "on"
    except Exception:
        pass
    record("fog/toggle → on", on, body)
    confirm("Is fog coming out NOW? (watch the machine)")
    print(f"  {DIM}waiting for the 6s fog burst to end…{RESET}")
    time.sleep(7)
    code, body = http("GET", f"{base}/fog/status")
    off = False
    try:
        off = json.loads(body).get("fog") == "off"
    except Exception:
        pass
    record("fog auto-stopped after duration", off, body)

    # 5. Upload — happy path ----------------------------------------------------
    section("5. Upload (the real ritual)")
    png = make_png(1280, 720, (200, 30, 160))  # vivid magenta, obvious on fog
    body_bytes, ctype = multipart_body(
        {"device_time": device_time()},
        ("file", "test.png", png, "image/png"),
    )
    code, resp = http("POST", f"{base}/upload", data=body_bytes, headers={"Content-Type": ctype})
    record("upload valid image → 200", code == 200, f"HTTP {code}")
    try:
        timing = json.loads(resp)
        heatup = timing.get("heatup_seconds", 5)
        dissolve = timing.get("dissolve_seconds", 30)
        record("upload response reports heatup/dissolve seconds",
               "heatup_seconds" in timing and "dissolve_seconds" in timing, resp)
    except Exception:
        heatup, dissolve = 5, 30
    print(f"  {DIM}fans + fog should start now; the image appears after a {heatup}s heat-up…{RESET}")
    confirm("Did fans + fog start immediately, then the magenta image appear after the heat-up?")

    # 6. Rejection paths (while the display from #5 is still busy) ---------------
    section("6. Rejection paths")
    # 6a. second valid upload during the display → 409 busy
    body_bytes, ctype = multipart_body(
        {"device_time": device_time()},
        ("file", "test2.png", png, "image/png"),
    )
    code, resp = http("POST", f"{base}/upload", data=body_bytes, headers={"Content-Type": ctype})
    retry = ""
    try:
        retry = f"retry_after={json.loads(resp).get('retry_after')}s"
    except Exception:
        pass
    record("second upload while busy → 409", code == 409, f"HTTP {code} {retry}")
    # 6b. non-image → 415
    body_bytes, ctype = multipart_body(
        {"device_time": device_time()},
        ("file", "notes.txt", b"i am not an image", "text/plain"),
    )
    code, _ = http("POST", f"{base}/upload", data=body_bytes, headers={"Content-Type": ctype})
    record("non-image file → 415", code == 415, f"HTTP {code}")
    # 6c. empty file → 400
    body_bytes, ctype = multipart_body(
        {"device_time": device_time()},
        ("file", "empty.png", b"", "image/png"),
    )
    code, _ = http("POST", f"{base}/upload", data=body_bytes, headers={"Content-Type": ctype})
    record("empty file → 400", code == 400, f"HTTP {code}")

    # 7. Let the display finish so we leave the server idle ----------------------
    section("7. Cooldown")
    fan_cooldown = 5  # FAN_COOLDOWN_SECS in image_upload_server.py
    wait_s = heatup + dissolve + fan_cooldown + 3  # + buffer
    print(f"  {DIM}waiting ~{wait_s}s for the heat-up + dissolve + fan cool-down to finish…{RESET}")
    time.sleep(wait_s)
    confirm("Has the projector gone black and the fans stopped?")
    print(f"\n  {DIM}On the Pi, confirm the log grew and the image was deleted:{RESET}")
    print(f"  {DIM}  tail -n 3 ~/fogscreen_uploads.jsonl{RESET}")

    summary()


def summary():
    print("\n" + "─" * 48)
    passed = sum(1 for _, ok in results if ok is True)
    failed = sum(1 for _, ok in results if ok is False)
    unknown = sum(1 for _, ok in results if ok is None)
    print(f"Summary: {GREEN}{passed} passed{RESET}, "
          f"{RED}{failed} failed{RESET}, {YELLOW}{unknown} unconfirmed{RESET}")
    if failed:
        print(f"{RED}Failures:{RESET}")
        for name, ok in results:
            if ok is False:
                print(f"  - {name}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
