#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/skill.local.env"

INSTALL_DIR=""
TABLE_URL=""
FEISHU_ACCESS_TOKEN=""

log() {
  printf '[batch-sync] %s\n' "$*"
}

fail() {
  printf '[batch-sync] ERROR: %s\n' "$*" >&2
  exit 1
}

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

load_skill_env() {
  [[ -f "$ENV_FILE" ]] || fail "Missing $ENV_FILE. Copy skill.local.env.example and fill it first."

  while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
    raw_line="${raw_line%$'\r'}"
    [[ -z "$(trim "$raw_line")" ]] && continue
    [[ "$(trim "$raw_line")" == \#* ]] && continue
    [[ "$raw_line" == *=* ]] || continue

    local key="${raw_line%%=*}"
    local value="${raw_line#*=}"
    key="$(trim "$key")"
    value="$(trim "$value")"

    case "$key" in
      INSTALL_DIR) INSTALL_DIR="$value" ;;
      TABLE_URL) TABLE_URL="$value" ;;
      FEISHU_ACCESS_TOKEN) FEISHU_ACCESS_TOKEN="$value" ;;
    esac
  done < "$ENV_FILE"

  [[ -n "$INSTALL_DIR" ]] || fail "INSTALL_DIR is required in $ENV_FILE."
  [[ -n "$TABLE_URL" ]] || fail "TABLE_URL is required in $ENV_FILE."
  [[ -n "$FEISHU_ACCESS_TOKEN" ]] || fail "FEISHU_ACCESS_TOKEN is required in $ENV_FILE."
}

check_cdp_ready() {
  local python_bin="$1"
  "$python_bin" - <<'PY'
import json
import sys
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=2) as response:
        payload = json.loads(response.read().decode("utf-8"))
    sys.exit(0 if payload.get("Browser") else 1)
except Exception:
    sys.exit(1)
PY
}

ensure_browser_ready() {
  local python_bin="$1"

  if check_cdp_ready "$python_bin"; then
    return 0
  fi

  log "Chrome CDP is not ready. Trying to start Chrome on port 9222."
  bash "$SCRIPT_DIR/start_browser_cdp.sh"

  local attempt
  for attempt in $(seq 1 15); do
    if check_cdp_ready "$python_bin"; then
      return 0
    fi
    sleep 1
  done

  fail "Chrome CDP did not become ready on http://127.0.0.1:9222."
}

main() {
  local run_mode="${1:-draft}"
  local max_records="${2:-0}"

  load_skill_env

  local cli_bin="$INSTALL_DIR/.venv/bin/automation-business-scaffold-run"
  local python_bin="$INSTALL_DIR/.venv/bin/python"

  [[ -x "$cli_bin" ]] || fail "Cannot find CLI at $cli_bin. Re-run the deployment script."
  [[ -x "$python_bin" ]] || fail "Cannot find Python at $python_bin. Re-run the deployment script."

  export FEISHU_ACCESS_TOKEN

  ensure_browser_ready "$python_bin"

  cd "$INSTALL_DIR"
  log "Running tiktok_feishu_batch_sync with run_mode=$run_mode max_records=$max_records"
  "$cli_bin" run \
    --task tiktok_feishu_batch_sync \
    --run-mode "$run_mode" \
    --param "table_url=$TABLE_URL" \
    --param "access_token_env=FEISHU_ACCESS_TOKEN" \
    --param "url_field_name=产品链接" \
    --param "profile_ref=local-chrome" \
    --param "max_records=$max_records"
}

main "$@"
