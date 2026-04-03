#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/skill.local.env"

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

load_skill_env() {
  [[ -f "$ENV_FILE" ]] || fail "Missing $ENV_FILE."

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

main() {
  load_skill_env

  local cleanup_script="$SCRIPT_DIR/run_cleanup.sh"
  local batch_script="$SCRIPT_DIR/run_batch_sync.sh"

  [[ -x "$cleanup_script" || -f "$cleanup_script" ]] || fail "Missing $cleanup_script."
  [[ -x "$batch_script" || -f "$batch_script" ]] || fail "Missing $batch_script."

  log "Step 1/2: normalizing and deduplicating TikTok links in Feishu"
  bash "$cleanup_script" canary

  log "Step 2/2: crawling TikTok competitor data and writing results back to Feishu"
  bash "$batch_script" canary 0
}

main "$@"
