#!/bin/sh
# Installer + starter for cf-auto-custom-hostname.
# Idempotent: safe to run on every container start / reboot.
#   - ensures /data/state.json exists (mounted volume, survives power outage)
#   - fetches app source from the public repo if this checkout lacks it
#   - installs deps, then starts the bot (restart policy handles recovery)
set -eu

APP_DIR="${APP_DIR:-/app}"
DATA_DIR="${DATA_DIR:-/data}"
APP_REPO_URL="${APP_REPO_URL:-https://github.com/rohjagad/cf-auto-custom-hostname.git}"

mkdir -p "$DATA_DIR"
if [ ! -f "$DATA_DIR/state.json" ]; then
  printf '{"domains": {}}\n' > "$DATA_DIR/state.json"
fi
chmod 600 "$DATA_DIR/state.json" 2>/dev/null || true

if [ ! -f "$APP_DIR/requirements.txt" ]; then
  echo "App source missing in $APP_DIR — fetching from $APP_REPO_URL ..."
  if [ -z "$APP_REPO_URL" ]; then
    echo "ERROR: set APP_REPO_URL to the public repo URL." >&2
    exit 1
  fi
  if ! command -v git >/dev/null 2>&1; then
    apk add --no-cache git >/dev/null 2>&1 || true
  fi
  if [ -z "$(ls -A "$APP_DIR" 2>/dev/null)" ]; then
    git clone --depth 1 "$APP_REPO_URL" "$APP_DIR"
  else
    _tmp="$(mktemp -d)"
    git clone --depth 1 "$APP_REPO_URL" "$_tmp" && cp -r "$_tmp"/. "$APP_DIR"/ && rm -rf "$_tmp"
  fi
fi

cd "$APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git pull --ff-only 2>/dev/null || true
fi

pip install --no-cache-dir -r requirements.txt

export DATA_DIR
export STATE_FILE="$DATA_DIR/state.json"
exec python -m bot
