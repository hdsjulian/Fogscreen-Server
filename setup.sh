#!/bin/bash
set -e

REPO_URL="https://github.com/hdsjulian/fogscreen-server"
REPO_DIR="/home/raspi/fogscreen-server"
VENV_DIR="/home/raspi/venv"
WEB_DIR="/var/www/html"
SERVICE_FILE="/etc/systemd/system/fogscreen.service"

echo "==> Installing system packages..."
sudo apt install -y python3-full nginx

echo "==> Cloning repo..."
if [ ! -d "$REPO_DIR/.git" ]; then
    git clone "$REPO_URL" "$REPO_DIR"
else
    git -C "$REPO_DIR" pull
fi

echo "==> Creating virtual environment..."
python3 -m venv "$VENV_DIR"

echo "==> Installing Python packages..."
"$VENV_DIR/bin/pip" install fastapi uvicorn pyserial pillow python-multipart

echo "==> Deploying HTML..."
sudo cp "$REPO_DIR/index.html" "$WEB_DIR/index.html"

echo "==> Installing systemd service..."
sudo cp "$REPO_DIR/fogscreen.service" "$SERVICE_FILE"
sudo systemctl daemon-reload
sudo systemctl enable fogscreen
sudo systemctl restart fogscreen

echo "==> Done. Status:"
sudo systemctl status fogscreen --no-pager
