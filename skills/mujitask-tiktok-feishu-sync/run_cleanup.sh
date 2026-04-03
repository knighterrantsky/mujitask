#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/skill.local.env"
RESULT_HELPER="$SCRIPT_DIR/openclaw_result.py"

INSTALL_DIR=""
TABLE_URL=""
FEISHU_ACCESS_TOKEN=""

log() {
  printf '[cleanup] %s\n' "$*"
}

fail() {
  printf '[cleanup] ERROR: %s\n' "$*" >&2
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

main() {
  local run_mode="${1:-draft}"

  load_skill_env

  local cli_bin="$INSTALL_DIR/.venv/bin/automation-business-scaffold-run"
  local python_bin="$INSTALL_DIR/.venv/bin/python"
  [[ -x "$cli_bin" ]] || fail "Cannot find CLI at $cli_bin. Re-run the deployment script."
  [[ -x "$python_bin" ]] || fail "Cannot find Python at $python_bin. Re-run the deployment script."
  [[ -f "$RESULT_HELPER" ]] || fail "Missing $RESULT_HELPER."

  export FEISHU_ACCESS_TOKEN

  local run_id
  local run_dir="$INSTALL_DIR/runtime/cli_runs"
  local stdout_dir="$run_dir/stdout"
  local run_file
  local steps_file
  local signals_file
  local stdout_file
  local cli_status=0
  local result_json=""
  local summary_text=""

  mkdir -p "$stdout_dir"

  run_id="$(printf 'openclaw-cleanup-%s-%s-%s' "$(date '+%Y%m%d%H%M%S')" "$$" "$RANDOM")"
  run_file="$run_dir/$run_id.json"
  steps_file="$run_dir/steps/$run_id.json"
  signals_file="$run_dir/signals/$run_id.json"
  stdout_file="$stdout_dir/$run_id.log"

  cd "$INSTALL_DIR"
  log "Running tiktok_product_link_cleanup with run_mode=$run_mode run_id=$run_id"
  log "Progress files: run_file=$run_file steps_file=$steps_file"
  log "CLI output: stdout_file=$stdout_file"

  if "$cli_bin" run \
    --task tiktok_product_link_cleanup \
    --run-mode "$run_mode" \
    --param "table_url=$TABLE_URL" \
    --param "access_token_env=FEISHU_ACCESS_TOKEN" \
    --param "url_field_name=产品链接" \
    --run-id "$run_id" \
    >"$stdout_file" 2>&1; then
    cli_status=0
  else
    cli_status=$?
  fi

  result_json="$("$python_bin" "$RESULT_HELPER" run-summary \
    --run-file "$run_file" \
    --steps-file "$steps_file" \
    --signals-file "$signals_file" \
    --stdout-file "$stdout_file" \
    --run-id "$run_id" \
    --fallback-task "tiktok_product_link_cleanup" \
    --status "$([[ $cli_status -eq 0 ]] && printf 'success' || printf 'failed')" \
    --error-message "$([[ $cli_status -eq 0 ]] && printf '' || printf 'tiktok_product_link_cleanup exited with code %s' "$cli_status")")"

  if [[ -n "${MUJITASK_RESULT_FILE:-}" ]]; then
    printf '%s\n' "$result_json" > "$MUJITASK_RESULT_FILE"
  fi

  summary_text="$("$python_bin" - "$result_json" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
print(payload.get("summary_text", ""))
PY
)"

  if [[ -n "$summary_text" ]]; then
    log "Summary: $summary_text"
  fi

  if (( cli_status == 0 )); then
    log "Completed run_id=$run_id"
  else
    log "Failed run_id=$run_id. Inspect $run_file and $stdout_file for details."
  fi

  if [[ "${MUJITASK_SUPPRESS_RESULT_MARKER:-0}" != "1" ]]; then
    printf '__OPENCLAW_RESULT__ %s\n' "$result_json"
  fi

  return "$cli_status"
}

main "$@"
