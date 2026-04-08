#!/usr/bin/env bash

set -euo pipefail

log() {
  printf '[browser-cdp] %s\n' "$*"
}

fail() {
  printf '[browser-cdp] ERROR: %s\n' "$*" >&2
  exit 1
}

detect_chrome_bin() {
  local candidates=(
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    "$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -x "$candidate" ]]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

probe_cdp_ready() {
  local port="$1"
  python3 - "$port" <<'PY'
import json
import sys
import urllib.request

port = sys.argv[1]
url = f"http://127.0.0.1:{port}/json/version"
try:
    with urllib.request.urlopen(url, timeout=2) as response:
        payload = json.loads(response.read().decode("utf-8"))
    raise SystemExit(0 if payload.get("Browser") else 1)
except Exception:
    raise SystemExit(1)
PY
}

stop_stale_cdp_instances() {
  local profile_dir="$1"
  local pids

  pids="$(pgrep -f -- "--user-data-dir=${profile_dir}" || true)"
  [[ -n "$pids" ]] || return 0

  log "Stopping stale Chrome instance(s) using profile ${profile_dir}"
  kill $pids 2>/dev/null || true
  sleep 2

  pids="$(pgrep -f -- "--user-data-dir=${profile_dir}" || true)"
  [[ -n "$pids" ]] || return 0

  log "Force killing stale Chrome instance(s) using profile ${profile_dir}"
  kill -9 $pids 2>/dev/null || true
  sleep 1
}

main() {
  local chrome_bin
  chrome_bin="$(detect_chrome_bin || true)"
  [[ -n "$chrome_bin" ]] || fail "Google Chrome was not found. Install Chrome and rerun deployment or this script."

  local port="${MUJITASK_CHROME_CDP_PORT:-9222}"
  local profile_dir="${MUJITASK_CHROME_PROFILE_DIR:-$HOME/.mujitask/chrome-cdp-profile}"
  mkdir -p "$profile_dir"

  if probe_cdp_ready "$port"; then
    log "Chrome CDP is already ready on port ${port}"
    return 0
  fi

  stop_stale_cdp_instances "$profile_dir"

  log "Starting Chrome with remote debugging on port ${port}"
  open -na "Google Chrome" --args \
    --remote-debugging-port="$port" \
    --remote-debugging-address=127.0.0.1 \
    --user-data-dir="$profile_dir" \
    --no-first-run \
    --no-default-browser-check
}

main "$@"
