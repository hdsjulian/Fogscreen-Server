#!/bin/bash
set -e

REPO_DIR="/home/raspi/fogscreen-server"
WEB_DIR="/var/www/html"

# Clone if not already present, otherwise pull
if [ ! -d "$REPO_DIR/.git" ]; then
    git clone https://github.com/hdsjulian/fogscreen-server "$REPO_DIR"
else
    git -C "$REPO_DIR" pull
fi

# Deploy HTML to nginx webroot
sudo cp "$REPO_DIR/index.html" "$WEB_DIR/index.html"

echo "Done."
