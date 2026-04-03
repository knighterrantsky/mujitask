#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/skill.local.env"

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

main() {
  local run_mode="${1:-draft}"

  load_skill_env

  local cli_bin="$INSTALL_DIR/.venv/bin/automation-business-scaffold-run"
  [[ -x "$cli_bin" ]] || fail "Cannot find CLI at $cli_bin. Re-run the deployment script."

  export FEISHU_ACCESS_TOKEN

  cd "$INSTALL_DIR"
  log "Running tiktok_product_link_cleanup with run_mode=$run_mode"
  "$cli_bin" run \
    --task tiktok_product_link_cleanup \
    --run-mode "$run_mode" \
    --param "table_url=$TABLE_URL" \
    --param "access_token_env=FEISHU_ACCESS_TOKEN" \
    --param "url_field_name=产品链接"
}

main "$@"
