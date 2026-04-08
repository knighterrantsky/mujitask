#!/usr/bin/env bash

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "[verify-openclaw] ERROR: This script currently supports macOS only." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_SKILL_DIR="$HOME/.openclaw/workspace/skills/mujitask-tiktok-feishu-sync"
SKILL_DIR="${1:-$DEFAULT_SKILL_DIR}"
ENV_FILE="$SKILL_DIR/skill.local.env"

INSTALL_DIR=""
TABLE_URL=""
FEISHU_ACCESS_TOKEN=""

log() {
  printf '[verify-openclaw] %s\n' "$*"
}

fail() {
  printf '[verify-openclaw] ERROR: %s\n' "$*" >&2
  exit 1
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

  [[ -n "$INSTALL_DIR" ]] || fail "INSTALL_DIR is missing in $ENV_FILE."
  [[ -n "$TABLE_URL" ]] || fail "TABLE_URL is missing in $ENV_FILE."
  [[ -n "$FEISHU_ACCESS_TOKEN" ]] || fail "FEISHU_ACCESS_TOKEN is missing in $ENV_FILE."
}

check_file() {
  local path="$1"
  [[ -e "$path" ]] || fail "Missing required path: $path"
  log "OK: $path"
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
    print(payload.get("Browser", ""))
except Exception:
    sys.exit(1)
PY
}

main() {
  load_skill_env

  local cli_bin="$INSTALL_DIR/.venv/bin/automation-business-scaffold-run"
  local python_bin="$INSTALL_DIR/.venv/bin/python"
  local browser_profiles="$INSTALL_DIR/config/browser_profiles.json"
  local deploy_state="$INSTALL_DIR/runtime/deployment/openclaw-deploy.env"
  local skills_root="$HOME/.openclaw/workspace/skills"

  log "Checking deployed skill directory"
  check_file "$SKILL_DIR"
  check_file "$SKILL_DIR/SKILL.md"
  check_file "$SKILL_DIR/skill.local.env"
  check_file "$SKILL_DIR/skill.local.env.example"
  check_file "$SKILL_DIR/run_cleanup_step.sh"
  check_file "$SKILL_DIR/run_pending_rows_step.sh"
  check_file "$SKILL_DIR/run_single_row_update_step.sh"
  check_file "$SKILL_DIR/run_keyword_candidate_step.sh"
  check_file "$SKILL_DIR/run_insert_seed_row_step.sh"
  check_file "$SKILL_DIR/run_fastmoss_login_check_step.sh"
  check_file "$SKILL_DIR/start_browser_cdp.sh"

  log "Checking installed project directory"
  check_file "$INSTALL_DIR"
  check_file "$cli_bin"
  check_file "$python_bin"
  check_file "$browser_profiles"
  check_file "$deploy_state"

  log "Checking SKILL.md frontmatter"
  check_skill_frontmatter "$SKILL_DIR/SKILL.md" "$python_bin"
  log "OK: SKILL.md frontmatter contains the required OpenClaw metadata"

  log "Checking OpenClaw workspace for obsolete skill backups"
  shopt -s nullglob
  local backup_dirs=("$skills_root"/mujitask-tiktok-feishu-sync.backup-*)
  shopt -u nullglob
  if ((${#backup_dirs[@]} > 0)); then
    fail "Found obsolete skill backup directories in OpenClaw workspace."
  fi
  log "OK: no obsolete OpenClaw skill backup directories were found"

  log "Checking list-tasks output"
  local tasks_json
  local cleanup_cmd
  tasks_json="$(mktemp)"
  # Expand the temp file path into the trap now; EXIT runs after local vars are gone.
  printf -v cleanup_cmd 'rm -f %q' "$tasks_json"
  trap "$cleanup_cmd" EXIT
  "$cli_bin" list-tasks > "$tasks_json"

  "$python_bin" - "$tasks_json" <<'PY'
import json
import sys

required = {"tiktok_product_link_cleanup", "tiktok_feishu_batch_sync"}
with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)

names = {item.get("name") for item in payload.get("tasks", []) if isinstance(item, dict)}
missing = sorted(required - names)
if missing:
    raise SystemExit(f"Missing tasks: {', '.join(missing)}")
print("OK")
PY
  log "OK: required tasks are present"

  log "Checking Chrome CDP availability"
  if browser_name="$(check_cdp_ready "$python_bin" 2>/dev/null)"; then
    log "OK: Chrome CDP is reachable at http://127.0.0.1:9222 (${browser_name:-unknown})"
  else
    log "WARN: Chrome CDP is not reachable at http://127.0.0.1:9222"
    log "You can start it with: bash \"$SKILL_DIR/start_browser_cdp.sh\""
  fi

  log "Verification completed"
  log "Skill directory: $SKILL_DIR"
  log "Install directory: $INSTALL_DIR"
  log "Table URL: $TABLE_URL"
}

main "$@"
