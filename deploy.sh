#!/bin/bash
set -e

REPO_DIR="/home/raspi/fogscreen-server"
WEB_DIR="/var/www/html"

# Switch to client mode to reach GitHub
~/toggle-wifi.sh 0

# Clone if not already present, otherwise pull
if [ ! -d "$REPO_DIR/.git" ]; then
    git clone https://github.com/hdsjulian/fogscreen-server "$REPO_DIR"
else
    git -C "$REPO_DIR" pull
fi

# Deploy HTML to nginx webroot
sudo cp "$REPO_DIR/index.html" "$WEB_DIR/index.html"

# Restart the FastAPI service
sudo systemctl restart fogscreen

# Switch back to AP mode
~/toggle-wifi.sh 1

echo "Done."
