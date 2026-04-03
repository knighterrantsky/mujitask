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

main() {
  local chrome_bin
  chrome_bin="$(detect_chrome_bin || true)"
  [[ -n "$chrome_bin" ]] || fail "Google Chrome was not found. Install Chrome and rerun deployment or this script."

  local profile_dir="${MUJITASK_CHROME_PROFILE_DIR:-$HOME/.mujitask/chrome-cdp-profile}"
  mkdir -p "$profile_dir"

  log "Starting Chrome with remote debugging on port 9222"
  nohup "$chrome_bin" \
    --remote-debugging-port=9222 \
    --user-data-dir="$profile_dir" \
    >/dev/null 2>&1 &
}

main "$@"
