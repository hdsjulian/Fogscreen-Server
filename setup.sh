#!/bin/bash
# ============================================================================
# Fogscreen — fresh-system provisioner
#
# Turns a VANILLA Raspberry Pi OS Lite (Bookworm, 64-bit) image on a Pi Zero 2W
# into a fully working fogscreen server: FastAPI app + nginx proxy + Xorg/feh
# projector display + WiFi access point with captive portal.
#
# Run ONCE on a fresh system. For routine code updates after a git push, use
# deploy.sh instead (this script and deploy.sh are intentionally separate).
#
# Bootstrap on a clean Pi:
#     curl -fsSL https://raw.githubusercontent.com/hdsjulian/fogscreen-server/main/setup.sh -o setup.sh
#     bash setup.sh
#
# Re-running is safe (idempotent): profiles/services are recreated, not duped.
# ============================================================================
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────
# CONFIG — edit these for your installation (hardcoded on purpose; one-time,
# public art piece). Leave AP_PASS empty ("") for an OPEN access point.
# ──────────────────────────────────────────────────────────────────────────
TARGET_USER="raspi"                       # owner of the service + files
WIFI_COUNTRY="US"                         # regulatory domain — AP won't start without it

AP_SSID="FogScreen"                       # the hotspot visitors connect to
AP_PASS=""                                # "" = open network; else >= 8 chars
AP_IP="192.168.4.1"                       # Pi's address while in AP mode

WLIAN_SSID="WLIAN"                        # secondary hotspot (toggle_wifi.sh)
WLIAN_PASS=""                             # "" = open

CLIENT_SSID="your-home-wifi"              # only used by deploy.sh to fetch updates
CLIENT_PASS="changeme"                    # set these to your real network for updates

REPO_URL="https://github.com/hdsjulian/fogscreen-server"
# ──────────────────────────────────────────────────────────────────────────

HOME_DIR="/home/$TARGET_USER"
REPO_DIR="$HOME_DIR/fogscreen-server"
VENV_DIR="$HOME_DIR/venv"
WEB_DIR="/var/www/html"
BLACK_PNG="$HOME_DIR/black.png"           # X idle loop (start-display.sh) shows this

# This script must run as root (or via sudo) so it can create users/services.
if [ "$(id -u)" -ne 0 ]; then
    echo "Re-running under sudo..."
    exec sudo -E bash "$0" "$@"
fi

run_as_user() { sudo -u "$TARGET_USER" "$@"; }

echo "==> [1/11] Ensuring user '$TARGET_USER' exists..."
if ! id "$TARGET_USER" &>/dev/null; then
    adduser --disabled-password --gecos "" "$TARGET_USER"
fi
# Groups needed for: sudo, DMX serial (/dev/ttyUSB*), GPIO, display, X on tty
for grp in sudo dialout gpio video tty input render; do
    getent group "$grp" >/dev/null 2>&1 && usermod -aG "$grp" "$TARGET_USER" || true
done

echo "==> [2/11] Installing system packages..."
export DEBIAN_FRONTEND=noninteractive
apt update
apt install -y \
    git python3-full python3-venv python3-pip \
    nginx network-manager dnsmasq \
    feh xserver-xorg xinit x11-xserver-utils \
    rfkill

echo "==> [3/11] Cloning / updating repo..."
if [ ! -d "$REPO_DIR/.git" ]; then
    run_as_user git clone "$REPO_URL" "$REPO_DIR"
else
    run_as_user git -C "$REPO_DIR" pull --ff-only
fi

echo "==> [4/11] Creating Python virtual environment + packages..."
run_as_user python3 -m venv "$VENV_DIR"
# rpi-hardware-pwm drives the two fan arrays via the SoC's hardware PWM
# (channels 0/1 → GPIO 18/19). No RPi.GPIO — there are no digital outputs left.
run_as_user "$VENV_DIR/bin/pip" install --upgrade pip
run_as_user "$VENV_DIR/bin/pip" install \
    fastapi uvicorn pyserial pillow python-multipart rpi-hardware-pwm

echo "==> [4b/11] Enabling hardware PWM (channels 0/1 → GPIO 18/19)..."
# Device-tree overlay: pwm-2chan maps PWM0→GPIO18 and PWM1→GPIO19 by default.
BOOT_CONFIG=/boot/firmware/config.txt
[ -f "$BOOT_CONFIG" ] || BOOT_CONFIG=/boot/config.txt
if ! grep -q "^dtoverlay=pwm-2chan" "$BOOT_CONFIG"; then
    printf '\n# Fogscreen: two hardware-PWM channels for the fan arrays\ndtoverlay=pwm-2chan\n' >> "$BOOT_CONFIG"
fi
# udev rule so the 'gpio' group (raspi is a member) can drive /sys/class/pwm
# without root — the service runs as $TARGET_USER, not root.
cat > /etc/udev/rules.d/99-pwm.rules <<'EOF'
SUBSYSTEM=="pwm*", PROGRAM="/bin/sh -c '\
    chown -R root:gpio /sys/class/pwm && chmod -R 770 /sys/class/pwm;\
    chown -R root:gpio /sys/devices/platform/soc/*.pwm/pwm/pwmchip* && chmod -R 770 /sys/devices/platform/soc/*.pwm/pwm/pwmchip*\
'"
EOF

echo "==> [5/11] Deploying web root + nginx config..."
install -d "$WEB_DIR"
cp "$REPO_DIR/index.html" "$WEB_DIR/index.html"
# Deploy the captive-portal/proxy config (old setup.sh skipped this, so /upload
# and the fan endpoints never reached uvicorn).
cp "$REPO_DIR/nginx.conf" /etc/nginx/sites-available/default
ln -sf /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
nginx -t

echo "==> [6/11] Setting up the X / feh display stack..."
# Generate the black idle image that start-display.sh loops on — never created
# before, so the Xorg session would error in a tight loop.
run_as_user "$VENV_DIR/bin/python3" -c \
    "from PIL import Image; Image.new('RGB',(1920,1080),(0,0,0)).save('$BLACK_PNG')"
chmod +x "$REPO_DIR/start-display.sh"
# Allow startx from a systemd service (no logged-in seat).
printf 'allowed_users=anybody\nneeds_root_rights=yes\n' > /etc/X11/Xwrapper.config
cp "$REPO_DIR/xorg.service" /etc/systemd/system/xorg.service

echo "==> [7/11] Installing the fogscreen service..."
cp "$REPO_DIR/fogscreen.service" /etc/systemd/system/fogscreen.service

echo "==> [8/11] Installing the wifi-toggle helper..."
# deploy.sh / operators call ~/toggle-wifi.sh (hyphen); repo file uses underscore.
cp "$REPO_DIR/toggle_wifi.sh" "$HOME_DIR/toggle-wifi.sh"
chmod +x "$HOME_DIR/toggle-wifi.sh"
chown "$TARGET_USER:$TARGET_USER" "$HOME_DIR/toggle-wifi.sh"

echo "==> [9/11] Configuring WiFi (AP + captive portal)..."
rfkill unblock wifi || true
# Set regulatory country (required for the AP to come up).
if command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_wifi_country "$WIFI_COUNTRY" || true
fi
iw reg set "$WIFI_COUNTRY" 2>/dev/null || true

# dnsmasq: DHCP + DNS for AP clients, with a catch-all DNS hijack so any domain
# resolves to the Pi (captive-portal detection then hits nginx → uvicorn → 204).
DHCP_LO="${AP_IP%.*}.10"
DHCP_HI="${AP_IP%.*}.200"
cat > /etc/dnsmasq.d/fogscreen.conf <<EOF
interface=wlan0
bind-dynamic
dhcp-range=$DHCP_LO,$DHCP_HI,255.255.255.0,24h
dhcp-option=3,$AP_IP
dhcp-option=6,$AP_IP
address=/#/$AP_IP
EOF

# (Re)create the three NetworkManager profiles the toggle script expects.
nm_ap() {  # $1=con-name  $2=ssid  $3=pass  $4=autoconnect(yes/no)  $5=priority
    nmcli connection delete "$1" >/dev/null 2>&1 || true
    nmcli connection add type wifi ifname wlan0 con-name "$1" ssid "$2" \
        autoconnect "$4"
    nmcli connection modify "$1" \
        802-11-wireless.mode ap 802-11-wireless.band bg \
        ipv4.method manual ipv4.addresses "$AP_IP/24" \
        connection.autoconnect-priority "$5"
    if [ -n "$3" ]; then
        nmcli connection modify "$1" \
            wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$3"
    fi
}
nm_ap ap    "$AP_SSID"    "$AP_PASS"    yes 10   # default boot mode
nm_ap wlian "$WLIAN_SSID" "$WLIAN_PASS" no  5

# Client profile — only brought up (by deploy.sh) to reach GitHub for updates.
nmcli connection delete mywifi >/dev/null 2>&1 || true
nmcli connection add type wifi ifname wlan0 con-name mywifi ssid "$CLIENT_SSID" \
    autoconnect no
[ -n "$CLIENT_PASS" ] && nmcli connection modify mywifi \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$CLIENT_PASS"
nmcli connection modify mywifi connection.autoconnect-priority 0

echo "==> [10/11] Fixing ownership + enabling services..."
chown -R "$TARGET_USER:$TARGET_USER" "$HOME_DIR"
systemctl daemon-reload
systemctl enable nginx dnsmasq xorg fogscreen

echo "==> [11/11] Starting services (network changes apply on reboot)..."
# Start app/display/web now. We deliberately DO NOT switch into AP mode live —
# that would drop an SSH session you're provisioning over. The 'ap' profile is
# autoconnect+top-priority, so a reboot brings the hotspot up cleanly.
systemctl restart nginx || true
systemctl restart xorg || true
systemctl restart fogscreen || true

cat <<EOF

============================================================================
 Done. Reboot to bring up the '$AP_SSID' access point:

     sudo reboot

 After reboot, connect a phone to SSID "$AP_SSID"$([ -z "$AP_PASS" ] && echo " (open)" || echo " (pass: $AP_PASS)").
 The captive portal opens the upload page automatically.

 Service status:  sudo systemctl status fogscreen --no-pager
 Switch modes:    $HOME_DIR/toggle-wifi.sh [ap|wlian|client]
 Update code:     bash $REPO_DIR/deploy.sh
============================================================================
EOF
