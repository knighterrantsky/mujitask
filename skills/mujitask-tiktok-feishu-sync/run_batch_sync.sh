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

normalize_env_entry() {
  local value
  value="$(trim "$1")"
  value="${value#$'\ufeff'}"
  if [[ "$value" == export\ * ]]; then
    value="$(trim "${value#export }")"
  fi
  if [[ ${#value} -ge 2 ]]; then
    if [[ "$value" == \"*\" && "$value" == *\" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
      value="${value:1:${#value}-2}"
    fi
  fi
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
    key="$(normalize_env_entry "$key")"
    value="$(normalize_env_entry "$value")"

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

generate_run_id() {
  printf 'openclaw-%s-%s-%s' "$(date '+%Y%m%d%H%M%S')" "$$" "$RANDOM"
}

read_progress_snapshot() {
  local python_bin="$1"
  local steps_file="$2"
  local run_file="$3"

  "$python_bin" - "$steps_file" "$run_file" <<'PY'
import json
import sys
from pathlib import Path

steps_path = Path(sys.argv[1])
run_path = Path(sys.argv[2])

step_count = 0
last_step = ""
last_status = ""
run_status = ""

if steps_path.exists():
    try:
        payload = json.loads(steps_path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            step_count = len(payload)
            if payload and isinstance(payload[-1], dict):
                last_step = str(payload[-1].get("step_id", "") or "")
                last_status = str(payload[-1].get("status", "") or "")
    except Exception:
        pass

if run_path.exists():
    try:
        payload = json.loads(run_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            run_status = str(payload.get("status", "") or "")
    except Exception:
        pass

print(f"{step_count}\t{last_step}\t{last_status}\t{run_status}")
PY
}

monitor_cli_progress() {
  local cli_pid="$1"
  local python_bin="$2"
  local steps_file="$3"
  local run_file="$4"
  local last_snapshot=""
  local heartbeat_counter=0

  while kill -0 "$cli_pid" 2>/dev/null; do
    local snapshot=""
    snapshot="$(read_progress_snapshot "$python_bin" "$steps_file" "$run_file" 2>/dev/null || true)"

    if [[ -n "$snapshot" && "$snapshot" != "$last_snapshot" ]]; then
      local step_count=""
      local last_step=""
      local last_status=""
      local run_status=""
      IFS=$'\t' read -r step_count last_step last_status run_status <<<"$snapshot"

      if [[ "${step_count:-0}" != "0" ]]; then
        log "Progress: run_status=${run_status:-running} completed_steps=${step_count} last_step=${last_step:-unknown} last_status=${last_status:-unknown}"
      elif [[ -n "${run_status:-}" ]]; then
        log "Progress: run_status=${run_status} waiting for workflow steps"
      fi

      last_snapshot="$snapshot"
      heartbeat_counter=0
    else
      heartbeat_counter=$((heartbeat_counter + 1))
      if (( heartbeat_counter % 3 == 0 )); then
        if [[ -f "$steps_file" || -f "$run_file" ]]; then
          log "Heartbeat: run is still active; waiting for the next workflow update"
        else
          log "Heartbeat: run is still active; waiting for runtime files to appear"
        fi
      fi
    fi

    sleep 5
  done
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

  local run_id
  local run_dir="$INSTALL_DIR/runtime/cli_runs"
  local run_file
  local steps_file
  local cli_pid
  local monitor_pid
  local cli_status=0

  run_id="$(generate_run_id)"
  run_file="$run_dir/$run_id.json"
  steps_file="$run_dir/steps/$run_id.json"

  cd "$INSTALL_DIR"
  log "Running tiktok_feishu_batch_sync with run_mode=$run_mode max_records=$max_records run_id=$run_id"
  log "Progress files: run_file=$run_file steps_file=$steps_file"

  "$cli_bin" run \
    --task tiktok_feishu_batch_sync \
    --run-mode "$run_mode" \
    --param "table_url=$TABLE_URL" \
    --param "access_token_env=FEISHU_ACCESS_TOKEN" \
    --param "url_field_name=产品链接" \
    --param "profile_ref=local-chrome" \
    --param "max_records=$max_records" \
    --run-id "$run_id" &
  cli_pid=$!

  monitor_cli_progress "$cli_pid" "$python_bin" "$steps_file" "$run_file" &
  monitor_pid=$!

  if wait "$cli_pid"; then
    cli_status=0
  else
    cli_status=$?
  fi

  wait "$monitor_pid" 2>/dev/null || true

  if (( cli_status != 0 )); then
    fail "tiktok_feishu_batch_sync failed with exit code $cli_status. Inspect $run_file and $steps_file for details."
  fi

  log "Completed run_id=$run_id"
}

main "$@"
