#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/skill.local.env"
RESULT_HELPER="$SCRIPT_DIR/openclaw_result.py"

INSTALL_DIR=""
TABLE_URL=""
FEISHU_ACCESS_TOKEN=""

log() {
  printf '[feishu-tiktok-sync] %s\n' "$*"
}

fail() {
  printf '[feishu-tiktok-sync] ERROR: %s\n' "$*" >&2
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
  [[ -f "$ENV_FILE" ]] || fail "Missing $ENV_FILE."

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
  local run_mode="${1:-canary}"
  local max_records="${2:-0}"
  local profile_ref="${3:-}"

  load_skill_env

  local cleanup_script="$SCRIPT_DIR/run_cleanup.sh"
  local batch_script="$SCRIPT_DIR/run_batch_sync.sh"
  local python_bin="$INSTALL_DIR/.venv/bin/python"
  local cleanup_result_file=""
  local batch_result_file=""
  local cleanup_status=0
  local batch_status=0
  local overall_json=""

  cleanup_result_file="$(mktemp)"
  batch_result_file="$(mktemp)"
  trap 'rm -f "$cleanup_result_file" "$batch_result_file"' EXIT

  [[ -x "$cleanup_script" || -f "$cleanup_script" ]] || fail "Missing $cleanup_script."
  [[ -x "$batch_script" || -f "$batch_script" ]] || fail "Missing $batch_script."
  [[ -x "$python_bin" ]] || fail "Cannot find Python at $python_bin. Re-run the deployment script."
  [[ -f "$RESULT_HELPER" ]] || fail "Missing $RESULT_HELPER."

  log "Step 1/2: normalizing and deduplicating TikTok links in Feishu"
  if MUJITASK_SUPPRESS_RESULT_MARKER=1 MUJITASK_RESULT_FILE="$cleanup_result_file" bash "$cleanup_script" "$run_mode"; then
    cleanup_status=0
  else
    cleanup_status=$?
  fi

  if (( cleanup_status != 0 )); then
    overall_json="$("$python_bin" "$RESULT_HELPER" combine \
      --cleanup-result-file "$cleanup_result_file" \
      --task-name "feishu_tiktok_sync" \
      --status "failed" \
      --message "TikTok Feishu sync stopped during cleanup." \
      --error-message "Cleanup stage failed.")"
    log "Step 1/2 failed. Final result marker follows."
    printf '__OPENCLAW_RESULT__ %s\n' "$overall_json"
    return "$cleanup_status"
  fi

  log "Step 2/2: crawling TikTok competitor data and writing results back to Feishu"
  if MUJITASK_SUPPRESS_RESULT_MARKER=1 MUJITASK_RESULT_FILE="$batch_result_file" bash "$batch_script" "$run_mode" "$max_records" "$profile_ref"; then
    batch_status=0
  else
    batch_status=$?
  fi

  if (( batch_status != 0 )); then
    overall_json="$("$python_bin" "$RESULT_HELPER" combine \
      --cleanup-result-file "$cleanup_result_file" \
      --batch-result-file "$batch_result_file" \
      --task-name "feishu_tiktok_sync" \
      --status "failed" \
      --message "TikTok Feishu sync stopped during batch sync." \
      --error-message "Batch sync stage failed.")"
    log "Step 2/2 failed. Final result marker follows."
    printf '__OPENCLAW_RESULT__ %s\n' "$overall_json"
    return "$batch_status"
  fi

  overall_json="$("$python_bin" "$RESULT_HELPER" combine \
    --cleanup-result-file "$cleanup_result_file" \
    --batch-result-file "$batch_result_file" \
    --task-name "feishu_tiktok_sync" \
    --status "success" \
    --message "TikTok Feishu sync completed.")"
  log "Finished end-to-end sync. Final result marker follows."
  printf '__OPENCLAW_RESULT__ %s\n' "$overall_json"
}

main "$@"
