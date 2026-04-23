#!/usr/bin/env bash

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "[verify-openclaw] ERROR: This script currently supports macOS only." >&2
  exit 1
fi

resolve_default_skill_dir() {
  if [[ -n "${OPENCLAW_SKILL_DIR:-}" ]]; then
    printf '%s\n' "${OPENCLAW_SKILL_DIR}"
    return 0
  fi

  local candidates=(
    "$HOME/.openclaw/workspace/skills/mujitask-tiktok-feishu-sync"
    "$HOME/.openclaw/workspace-tiktok/skills/mujitask-tiktok-feishu-sync"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -d "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  printf '%s\n' "${candidates[0]}"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="${1:-$(resolve_default_skill_dir)}"
ENV_FILE="$SKILL_DIR/skill.local.env"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"

INSTALL_DIR=""
TABLE_URL=""
FEISHU_ACCESS_TOKEN=""
EXECUTOR_ENV_FILE=""

log() {
  printf '[verify-openclaw] %s\n' "$*"
}

fail() {
  printf '[verify-openclaw] ERROR: %s\n' "$*" >&2
  exit 1
}

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

normalize_kv_entry() {
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

read_env_value() {
  local file_path="$1"
  local target_key="$2"

  [[ -f "$file_path" ]] || return 1

  while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
    raw_line="${raw_line%$'\r'}"
    [[ -z "$(trim "$raw_line")" ]] && continue
    [[ "$(trim "$raw_line")" == \#* ]] && continue
    [[ "$raw_line" == *=* ]] || continue

    local key="${raw_line%%=*}"
    local value="${raw_line#*=}"
    key="$(normalize_kv_entry "$key")"
    value="$(normalize_kv_entry "$value")"
    if [[ "$key" == "$target_key" ]]; then
      printf '%s' "$value"
      return 0
    fi
  done < "$file_path"

  return 1
}

check_skill_frontmatter() {
  local skill_md="$1"
  local python_bin="$2"

  "$python_bin" - "$skill_md" <<'PY'
from pathlib import Path
import re
import sys

skill_md = Path(sys.argv[1])
text = skill_md.read_text(encoding="utf-8")

match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, flags=re.DOTALL)
if not match:
    raise SystemExit(f"{skill_md} is missing YAML frontmatter.")

frontmatter = match.group(1)
name_match = re.search(r"(?m)^\s*name:\s*(\S.*?)\s*$", frontmatter)
description_match = re.search(r"(?m)^\s*description:\s*.+$", frontmatter)

if not name_match:
    raise SystemExit(f"{skill_md} frontmatter is missing name.")
if name_match.group(1).strip() != "mujitask-tiktok-feishu-sync":
    raise SystemExit(f"{skill_md} frontmatter name must be mujitask-tiktok-feishu-sync.")
if not description_match:
    raise SystemExit(f"{skill_md} frontmatter is missing description.")
PY
}

check_file() {
  local path="$1"
  [[ -e "$path" ]] || fail "Missing required path: $path"
  log "OK: $path"
}

load_skill_env() {
  [[ -f "$ENV_FILE" ]] || fail "Missing $ENV_FILE."

  INSTALL_DIR="$(read_env_value "$ENV_FILE" "INSTALL_DIR" 2>/dev/null || true)"
  TABLE_URL="$(read_env_value "$ENV_FILE" "TABLE_URL" 2>/dev/null || true)"
  FEISHU_ACCESS_TOKEN="$(read_env_value "$ENV_FILE" "FEISHU_ACCESS_TOKEN" 2>/dev/null || true)"

  [[ -n "$INSTALL_DIR" ]] || fail "INSTALL_DIR is missing in $ENV_FILE."
  [[ -n "$TABLE_URL" ]] || fail "TABLE_URL is missing in $ENV_FILE."
  [[ -n "$FEISHU_ACCESS_TOKEN" ]] || fail "FEISHU_ACCESS_TOKEN is missing in $ENV_FILE."
}

main() {
  load_skill_env

  EXECUTOR_ENV_FILE="$INSTALL_DIR/scripts/execution_control/executor.local.env"

  local cli_bin="$INSTALL_DIR/.venv/bin/automation-business-scaffold-run"
  local python_bin="$INSTALL_DIR/.venv/bin/python"
  local deploy_state="$INSTALL_DIR/runtime/deployment/openclaw-deploy.env"
  log "Checking deployed skill directory"
  check_file "$SKILL_DIR"
  check_file "$SKILL_DIR/SKILL.md"
  check_file "$SKILL_DIR/skill.local.env"
  check_file "$SKILL_DIR/skill.local.env.example"
  check_file "$SKILL_DIR/run_refresh_current_competitor_table_step.sh"
  check_file "$SKILL_DIR/run_keyword_search_step.sh"
  check_file "$SKILL_DIR/run_skill_step.py"
  check_file "$SKILL_DIR/lightweight_submit.py"

  log "Checking installed project directory"
  check_file "$INSTALL_DIR"
  check_file "$cli_bin"
  check_file "$python_bin"
  check_file "$EXECUTOR_ENV_FILE"
  check_file "$INSTALL_DIR/scripts/execution_control/install_launch_agents.sh"
  check_file "$INSTALL_DIR/scripts/execution_control/run_launchd_agent.sh"
  check_file "$INSTALL_DIR/config/deployment/launchd/com.happyzhao.mujitask.executor-daemon.plist.template"
  check_file "$INSTALL_DIR/config/deployment/launchd/com.happyzhao.mujitask.browser-runloop.plist.template"
  check_file "$INSTALL_DIR/config/deployment/launchd/com.happyzhao.mujitask.outbox-dispatcher.plist.template"
  if [[ -f "$INSTALL_DIR/config/browser_profiles.json" ]]; then
    log "OK: $INSTALL_DIR/config/browser_profiles.json"
  else
    log "WARN: browser_profiles.json is missing; this is acceptable when the deployment uses Roxy/browser bridge instead of local chrome_cdp."
  fi

  if [[ -f "$deploy_state" ]]; then
    log "Checking deployment state"
    local install_layout_version update_supported
    install_layout_version="$(read_env_value "$deploy_state" "INSTALL_LAYOUT_VERSION" 2>/dev/null || true)"
    update_supported="$(read_env_value "$deploy_state" "UPDATE_SUPPORTED" 2>/dev/null || true)"
    [[ "$install_layout_version" == "1" ]] || fail "INSTALL_LAYOUT_VERSION=1 is missing in $deploy_state."
    [[ "$update_supported" == "1" ]] || fail "UPDATE_SUPPORTED=1 is missing in $deploy_state."
    log "OK: deployment state marks this install as update compatible"
  else
    log "WARN: $deploy_state is missing; treating this as a manually prepared install rather than a deploy-openclaw managed install."
  fi

  log "Checking SKILL.md frontmatter"
  check_skill_frontmatter "$SKILL_DIR/SKILL.md" "$python_bin"
  log "OK: SKILL.md frontmatter contains the required OpenClaw metadata"

  log "Checking list-tasks output"
  local tasks_json
  local cleanup_cmd
  tasks_json="$(mktemp)"
  printf -v cleanup_cmd 'rm -f %q' "$tasks_json"
  trap "$cleanup_cmd" EXIT
  "$cli_bin" list-tasks > "$tasks_json"
  "$python_bin" - "$tasks_json" <<'PY'
import json
import sys

required = {
    "refresh_current_competitor_table",
    "search_keyword_competitor_products",
    "feishu_single_row_update",
    "fastmoss_keyword_candidate_discovery",
}

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)

names = {item.get("name") for item in payload.get("tasks", []) if isinstance(item, dict)}
missing = sorted(required - names)
if missing:
    raise SystemExit(f"Missing tasks: {', '.join(missing)}")
print("OK")
PY
  log "OK: current top-level and leaf tasks are present"

  log "Checking launchd installation"
  local labels=(
    "com.happyzhao.mujitask.executor-daemon"
    "com.happyzhao.mujitask.browser-runloop"
    "com.happyzhao.mujitask.outbox-dispatcher"
  )
  local label
  for label in "${labels[@]}"; do
    check_file "${LAUNCH_AGENTS_DIR}/${label}.plist"
    launchctl list | grep -q "$label" || fail "launchd service is not loaded: $label"
    log "OK: launchd service is loaded: $label"
  done

  log "Checking runtime dependencies from executor.local.env"
  (
    set -a
    # shellcheck disable=SC1090
    source "$EXECUTOR_ENV_FILE"
    set +a
    "$python_bin" - <<'PY'
import os

from minio import Minio
from sqlalchemy import create_engine, inspect

db_url = os.environ.get("BUSINESS_EXECUTION_CONTROL_DB_URL", "").strip()
if not db_url:
    raise SystemExit("executor.local.env must set BUSINESS_EXECUTION_CONTROL_DB_URL")

engine = create_engine(db_url, future=True, pool_pre_ping=True)
inspector = inspect(engine)
required_tables = {
    "task_request",
    "task_execution",
    "resource_lease",
    "notification_outbox",
    "artifact_object",
    "entity_registry",
    "external_binding",
    "entity_snapshot",
}
names = set(inspector.get_table_names())
missing = sorted(required_tables - names)
if missing:
    raise SystemExit(f"Missing runtime tables: {', '.join(missing)}")

provider = os.environ.get("BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER", "").strip().lower()
bucket = os.environ.get("BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET", "").strip()
if provider == "minio":
    endpoint = os.environ.get("BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT", "").strip()
    access_key = os.environ.get("BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY", "").strip()
    secure = os.environ.get("BUSINESS_EXECUTION_CONTROL_MINIO_SECURE", "").strip().lower() in {"1", "true", "yes", "on"}
    if not endpoint or not access_key or not secret_key or not bucket:
        raise SystemExit("MinIO provider is enabled but endpoint/access_key/secret_key/bucket is incomplete.")
    client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
    if not client.bucket_exists(bucket):
        raise SystemExit(f"Configured MinIO bucket does not exist: {bucket}")
print("OK")
PY
  )
  log "OK: database schema and object storage are reachable"

  log "Verification completed"
  log "Skill directory: $SKILL_DIR"
  log "Install directory: $INSTALL_DIR"
  log "Table URL: $TABLE_URL"
}

main "$@"
