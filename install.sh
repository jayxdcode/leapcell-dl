#!/usr/bin/env bash
set -euo pipefail

# Non-interactive installation script (Debian/Ubuntu or Alpine)
# MUST export MEGA_USER and MEGA_PASS in env before running.
# Optional env:
#   RCLONE_REMOTE (default: mega)
#   RCLONE_REMOTE_FOLDER (default: leapcell_cache)
#   APP_DIR (default: current dir)
# Example:
#   MEGA_USER='you@mega.nz' MEGA_PASS='yourpassword' ./install.sh

: "${MEGA_USER:?Please set MEGA_USER env var (mega.nz username/email)}"
: "${MEGA_PASS:?Please set MEGA_PASS env var (mega.nz password)}"

RCLONE_REMOTE="${RCLONE_REMOTE:-mega}"
RCLONE_REMOTE_FOLDER="${RCLONE_REMOTE_FOLDER:-leapcell_cache}"
APP_DIR="${APP_DIR:-$(pwd)}"

echo "APP_DIR = $APP_DIR"
echo "RCLONE_REMOTE = $RCLONE_REMOTE"
echo "RCLONE_REMOTE_FOLDER = $RCLONE_REMOTE_FOLDER"

# detect OS family
if [ -f /etc/alpine-release ]; then
  PKG="apk"
elif command -v apt-get >/dev/null 2>&1; then
  PKG="apt"
elif command -v yum >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1; then
  PKG="yum"
else
  echo "Unsupported distro (no apt/yum/apk). You must install prerequisites manually."
  PKG="unknown"
fi

install_packages_debian() {
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    curl ca-certificates git build-essential python3 python3-venv python3-pip \
    redis-server unzip gnupg apt-transport-https \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxss1 libxcomposite1 \
    libxrandr2 libgbm1 libasound2 libpangocairo-1.0-0 libx11-xcb1 libgtk-3-0
}

install_packages_alpine() {
  apk update
  apk add --no-cache curl ca-certificates git build-base python3 py3-venv py3-pip redis \
    libstdc++ libx11 libxrandr libxcomposite libxss libgbm alsa-lib atk \
    gtk+3 nss
}

install_packages_yum() {
  yum install -y epel-release
  yum install -y curl ca-certificates git gcc gcc-c++ make python3 python3-pip python3-venv redis
}

case "$PKG" in
  apt)  echo "Detected apt-based distro. Installing packages..."; install_packages_debian ;;
  apk)  echo "Detected Alpine. Installing packages..."; install_packages_alpine ;;
  yum)  echo "Detected yum-based distro. Installing packages..."; install_packages_yum ;;
  *)    echo "Skipping package install. Please install python3, pip, redis-server, curl, rclone prerequisites manually."; ;;
esac

# install rclone (official script) - try with sudo if available
echo "Installing rclone..."
if command -v sudo >/dev/null 2>&1; then
  curl https://rclone.org/install.sh | sudo bash
else
  curl https://rclone.org/install.sh | bash
fi

# create rclone config directory and write non-interactive config for mega
RCLONE_CONF_DIR="${HOME}/.config/rclone"
mkdir -p "${RCLONE_CONF_DIR}"
RCLONE_CONF_FILE="${RCLONE_CONF_DIR}/rclone.conf"

echo "Writing rclone config to ${RCLONE_CONF_FILE} for remote '${RCLONE_REMOTE}' (mega)"
cat > "${RCLONE_CONF_FILE}" <<EOF
[${RCLONE_REMOTE}]
type = mega
user = ${MEGA_USER}
pass = ${MEGA_PASS}
EOF

chmod 600 "${RCLONE_CONF_FILE}"
echo "rclone config written. (File perms 600)"

# test rclone remote list (best-effort)
echo "Testing rclone remote (lsd)..."
if rclone lsd "${RCLONE_REMOTE}:" >/dev/null 2>&1; then
  echo "rclone remote '${RCLONE_REMOTE}' reachable."
else
  echo "Warning: rclone remote '${RCLONE_REMOTE}' not reachable or empty. That's OK if credentials need a moment to sync — you can test manually with: rclone lsd ${RCLONE_REMOTE}:"
fi

# create app dir and python venv
cd "${APP_DIR}"
echo "Setting up python venv in ${APP_DIR}/venv ..."
python3 -m venv venv
# shellcheck disable=SC1091
. venv/bin/activate

python -m pip install --upgrade pip
echo "Installing python deps (fastapi, uvicorn, playwright, redis, aiofiles)..."
pip install "fastapi" "uvicorn[standard]" "redis" "aiofiles" "playwright"

# install playwright chromium browser (non-interactive)
echo "Installing Playwright browsers (chromium)... This will download Chromium."
python -m playwright install chromium

# Ensure redis-server running (try systemctl or start manually)
echo "Ensuring redis-server is running..."
if command -v systemctl >/dev/null 2>&1; then
  if sudo systemctl enable --now redis-server; then
    echo "redis-server started via systemctl."
  else
    echo "systemctl didn't start redis-server; trying 'redis-server --daemonize yes' fallback."
    redis-server --daemonize yes || true
  fi
else
  # fallback: try to start redis-server as background daemon
  if command -v redis-server >/dev/null 2>&1; then
    redis-server --daemonize yes || true
    echo "Started redis-server (daemonize)."
  else
    echo "redis-server not found. Install redis-server and start it manually."
  fi
fi

echo
echo "INSTALLATION COMPLETE."
echo "Next steps:"
echo "  - Put your FastAPI app (app.py) in ${APP_DIR} (if not already)."
echo "  - Edit environment variables as needed, then run ./run.sh"
echo
echo "Example run (one-liner):"
echo "  export SERVICE_URL_TEMPLATE='https://leapcell.example/item/{id}'"
echo "  export RCLONE_REMOTE='${RCLONE_REMOTE}'"
echo "  export RCLONE_REMOTE_FOLDER='${RCLONE_REMOTE_FOLDER}'"
echo "  export REDIS_URL='redis://localhost:6379/0'"
echo "  ./run.sh"
