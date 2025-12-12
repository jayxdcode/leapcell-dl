#!/usr/bin/env bash
set -euo pipefail

# run.sh - run the FastAPI app inside the venv
# Ensure you exported MEGA_USER and MEGA_PASS before install.sh; you don't need to export them at runtime
# Required env for runtime:
#   SERVICE_URL_TEMPLATE (eg "https://leapcell.example/item/{id}")
# Optional:
#   RCLONE_REMOTE (default: mega)
#   RCLONE_REMOTE_FOLDER (default: leapcell_cache)
#   BROWSER_EXECUTABLE_PATH (if you want to point to a system chromium)
#   REDIS_URL (default redis://localhost:6379/0)
# Example:
#   SERVICE_URL_TEMPLATE='https://example/item/{id}' ./run.sh

SERVICE_URL_TEMPLATE="${SERVICE_URL_TEMPLATE:-}"
RCLONE_REMOTE="${RCLONE_REMOTE:-mega}"
RCLONE_REMOTE_FOLDER="${RCLONE_REMOTE_FOLDER:-leapcell_cache}"
REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
BROWSER_EXECUTABLE_PATH="${BROWSER_EXECUTABLE_PATH:-}"

if [ -z "$SERVICE_URL_TEMPLATE" ]; then
  echo "Error: SERVICE_URL_TEMPLATE must be set (eg export SERVICE_URL_TEMPLATE='https://site/item/{id}')"
  exit 2
fi

# Activate venv
if [ ! -f "venv/bin/activate" ]; then
  echo "venv not found. Run ./install.sh first."
  exit 3
fi

# shellcheck disable=SC1091
. venv/bin/activate

export SERVICE_URL_TEMPLATE
export RCLONE_REMOTE
export RCLONE_REMOTE_FOLDER
export REDIS_URL
export BROWSER_EXECUTABLE_PATH

# Run uvicorn (single worker) and bind to 0.0.0.0:8000
echo "Starting FastAPI (uvicorn) on 0.0.0.0:8000 ..."
uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1
