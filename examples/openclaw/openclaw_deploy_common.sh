#!/usr/bin/env bash

set -euo pipefail

: "${OPENCLAW_LOG_PREFIX:=openclaw}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "[${OPENCLAW_LOG_PREFIX}] ERROR: This script currently supports macOS only." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENCLAW_DEPLOY_UTILS="$SCRIPT_DIR/openclaw_deploy_utils.py"
TMP_ROOT="${TMP_ROOT:-$(mktemp -d)}"
trap 'rm -rf "$TMP_ROOT"' EXIT

UV_BIN=""
PYTHON_BIN=""
GITHUB_AUTH_TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"

log() {
  printf '[%s] %s\n' "$OPENCLAW_LOG_PREFIX" "$*"
}

warn() {
  printf '[%s] WARN: %s\n' "$OPENCLAW_LOG_PREFIX" "$*" >&2
}

fail() {
  printf '[%s] ERROR: %s\n' "$OPENCLAW_LOG_PREFIX" "$*" >&2
  exit 1
}

check_skill_frontmatter() {
  local skill_md="$1"

  "$PYTHON_BIN" - "$skill_md" <<'PY'
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
if name_match.group(1).strip().strip("\"'") != "mujitask-tiktok-feishu-sync":
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

to_lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

prompt() {
  local label="$1"
  local default_value="${2:-}"
  local result=""
  if [[ -n "$default_value" ]]; then
    read -r -p "$label [$default_value]: " result
    result="$(trim "$result")"
    if [[ -z "$result" ]]; then
      result="$default_value"
    fi
  else
    while [[ -z "$result" ]]; do
      read -r -p "$label: " result
      result="$(trim "$result")"
    done
  fi
  printf '%s' "$result"
}

prompt_optional() {
  local label="$1"
  local default_value="${2:-}"
  local result=""

  if [[ -n "$default_value" ]]; then
    read -r -p "$label [$default_value]: " result
    result="$(trim "$result")"
    if [[ -z "$result" ]]; then
      result="$default_value"
    fi
  else
    read -r -p "$label: " result
    result="$(trim "$result")"
  fi

  printf '%s' "$result"
}

prompt_secret() {
  local label="$1"
  local result=""
  while [[ -z "$result" ]]; do
    read -r -s -p "$label: " result
    printf '\n'
    result="$(trim "$result")"
  done
  printf '%s' "$result"
}

prompt_secret_optional() {
  local label="$1"
  local result=""
  read -r -s -p "$label: " result
  printf '\n'
  result="$(trim "$result")"
  printf '%s' "$result"
}

quote_env_value() {
  local raw_value="${1-}"
  "$PYTHON_BIN" - "$raw_value" <<'PY'
import shlex
import sys

print(shlex.quote(sys.argv[1]))
PY
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "$1 is required."
}

resolve_uv_bin() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  if [[ -x "$HOME/.local/bin/uv" ]]; then
    printf '%s\n' "$HOME/.local/bin/uv"
    return 0
  fi
  if [[ -x "$HOME/.cargo/bin/uv" ]]; then
    printf '%s\n' "$HOME/.cargo/bin/uv"
    return 0
  fi
  return 1
}

ensure_uv() {
  require_command curl

  if UV_BIN="$(resolve_uv_bin 2>/dev/null)"; then
    return 0
  fi

  log "Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh

  UV_BIN="$(resolve_uv_bin 2>/dev/null || true)"
  [[ -n "$UV_BIN" ]] || fail "uv installation finished but uv was not found in PATH or common install paths."
}

ensure_python_311() {
  log "Ensuring Python 3.11 is available through uv"
  "$UV_BIN" python install 3.11 >/dev/null
  PYTHON_BIN="$("$UV_BIN" python find --managed-python --no-project --resolve-links 3.11 | tr -d '\r' | head -n 1)"
  [[ -n "$PYTHON_BIN" ]] || fail "Could not resolve Python 3.11 after uv installation."
  [[ -x "$PYTHON_BIN" ]] || fail "Resolved Python 3.11 is not executable: $PYTHON_BIN"
}

ensure_node_runtime() {
  if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
    return 0
  fi
  if command -v brew >/dev/null 2>&1; then
    log "Installing Node.js runtime with Homebrew"
    brew install node
  fi
  command -v node >/dev/null 2>&1 || fail "node is required for FastMoss chart rendering."
  command -v npm >/dev/null 2>&1 || fail "npm is required for FastMoss chart rendering dependencies."
}

install_project_node_dependencies() {
  local install_dir="$1"
  local package_json="$install_dir/package.json"
  [[ -f "$package_json" ]] || return 0

  ensure_node_runtime
  if [[ -f "$install_dir/package-lock.json" ]]; then
    log "Installing project Node.js runtime dependencies with npm ci"
    (cd "$install_dir" && npm ci --omit=dev --no-audit --no-fund)
  else
    log "Installing project Node.js runtime dependencies with npm install"
    (cd "$install_dir" && npm install --omit=dev --no-audit --no-fund)
  fi
}

validate_project_node_dependencies() {
  local install_dir="$1"
  local python_bin="$2"
  local package_json="$install_dir/package.json"
  [[ -f "$package_json" ]] || fail "Smoke check failed: $package_json is missing."

  ensure_node_runtime
  NODE_BINARY="$(command -v node)" \
  FASTMOSS_VISUALIZATION_RENDERER_PACKAGE_JSON="$package_json" \
    "$python_bin" - <<'PY'
from automation_business_scaffold.infrastructure.fastmoss.visualization_renderer import (
    FastMossVisualizationRenderer,
)

FastMossVisualizationRenderer().validate_runtime_dependencies()
print("OK")
PY
}

python_json_get() {
  local query="$1"
  local input_file="$2"
  "$PYTHON_BIN" - "$query" "$input_file" <<'PY'
import json
import sys

query = sys.argv[1]
input_path = sys.argv[2]
with open(input_path, "r", encoding="utf-8") as handle:
    payload = json.load(handle)

value = payload
for part in query.split("."):
    if isinstance(value, list):
        value = value[int(part)]
    else:
        value = value.get(part)
    if value is None:
        break

if isinstance(value, str):
    print(value)
elif value is not None:
    print(json.dumps(value, ensure_ascii=False))
PY
}

parse_github_slug() {
  local repo_url="$1"
  "$PYTHON_BIN" - "$repo_url" <<'PY'
import re
import sys

repo_url = sys.argv[1].strip()
patterns = [
    r"^(?:git\+)?https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$",
    r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$",
]

for pattern in patterns:
    match = re.match(pattern, repo_url)
    if match:
        print(f"{match.group(1)}/{match.group(2)}")
        raise SystemExit(0)

raise SystemExit(1)
PY
}

resolve_latest_github_ref() {
  local slug="$1"
  local latest_json="$TMP_ROOT/github-latest.json"
  local tags_json="$TMP_ROOT/github-tags.json"

  if github_api_download "https://api.github.com/repos/$slug/releases/latest" "$latest_json"; then
    local tag_name
    tag_name="$(python_json_get "tag_name" "$latest_json" | tr -d '\r')"
    if [[ -n "$tag_name" && "$tag_name" != "null" ]]; then
      printf '%s' "$tag_name"
      return 0
    fi
  fi

  github_api_download "https://api.github.com/repos/$slug/tags?per_page=1" "$tags_json" \
    || fail_github_download "Could not resolve latest tag for $slug."
  local tag_name
  tag_name="$(python_json_get "0.name" "$tags_json" | tr -d '\r')"
  [[ -n "$tag_name" && "$tag_name" != "null" ]] || fail "The repository $slug does not expose a latest release or tag."
  printf '%s' "$tag_name"
}

github_api_download() {
  local url="$1"
  local target="$2"

  local curl_args=(
    -fsSL
    -H "Accept: application/vnd.github+json"
  )

  if [[ -n "$GITHUB_AUTH_TOKEN" ]]; then
    curl_args+=(
      -H "Authorization: Bearer $GITHUB_AUTH_TOKEN"
      -H "X-GitHub-Api-Version: 2022-11-28"
    )
  fi

  curl "${curl_args[@]}" "$url" -o "$target"
}

fail_github_download() {
  local base_message="$1"

  if [[ -z "$GITHUB_AUTH_TOKEN" ]]; then
    fail "$base_message If the GitHub repository is private, rerun and provide a GitHub PAT, or set GITHUB_TOKEN / GH_TOKEN."
  fi

  fail "$base_message GitHub PAT was provided, so verify that the token can read the target repository."
}

download_file() {
  local url="$1"
  local target="$2"

  if [[ "$url" == https://api.github.com/* ]]; then
    github_api_download "$url" "$target" || fail_github_download "Failed to download GitHub archive from $url."
    return 0
  fi

  curl -fsSL "$url" -o "$target" || fail "Failed to download archive from $url."
}

extract_archive() {
  local archive_path="$1"
  local output_dir="$2"

  "$PYTHON_BIN" - "$archive_path" "$output_dir" <<'PY'
from pathlib import Path
import shutil
import sys
import tarfile
import zipfile

archive_path = Path(sys.argv[1])
output_dir = Path(sys.argv[2])
if output_dir.exists():
    shutil.rmtree(output_dir)
output_dir.mkdir(parents=True, exist_ok=True)

if zipfile.is_zipfile(archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(output_dir)
elif tarfile.is_tarfile(archive_path):
    with tarfile.open(archive_path) as archive:
        archive.extractall(output_dir)
else:
    raise SystemExit(f"Unsupported archive format: {archive_path}")

children = [item for item in output_dir.iterdir()]
root = children[0] if len(children) == 1 and children[0].is_dir() else output_dir
print(root)
PY
}

resolve_openclaw_skills_dir() {
  if [[ -n "${OPENCLAW_SKILLS_DIR:-}" ]]; then
    printf '%s' "${OPENCLAW_SKILLS_DIR}"
    return 0
  fi

  local default_dir="$HOME/.openclaw/workspace/skills"
  local alternate_dir="$HOME/.openclaw/workspace-tiktok/skills"

  if [[ -d "$default_dir" ]]; then
    printf '%s' "$default_dir"
    return 0
  fi
  if [[ -d "$alternate_dir" ]]; then
    printf '%s' "$alternate_dir"
    return 0
  fi

  printf '%s' "$default_dir"
}

replace_target_dir() {
  local target_dir="$1"
  if [[ -e "$target_dir" ]]; then
    log "Existing directory detected, removing it before replacement: $target_dir"
    rm -rf "$target_dir"
  fi
  mkdir -p "$(dirname "$target_dir")"
  mkdir -p "$target_dir"
}

read_project_dependencies() {
  local pyproject_path="$1"
  "$PYTHON_BIN" - "$pyproject_path" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    data = tomllib.load(handle)

for dep in data.get("project", {}).get("dependencies", []):
    if dep.startswith("automation-framework @ "):
        continue
    print(dep)
PY
}

read_framework_dependency_json() {
  local pyproject_path="$1"
  "$PYTHON_BIN" "$OPENCLAW_DEPLOY_UTILS" read-framework-dependency --path "$pyproject_path"
}

LAST_FRAMEWORK_ARCHIVE_URL=""

install_framework_from_pyproject() {
  local pyproject_path="$1"
  local venv_python="$2"
  local framework_json="$TMP_ROOT/framework-dependency.json"

  LAST_FRAMEWORK_ARCHIVE_URL=""
  read_framework_dependency_json "$pyproject_path" > "$framework_json"

  local framework_kind framework_source
  framework_kind="$(python_json_get "kind" "$framework_json" | tr -d '\r')"
  framework_source="$(python_json_get "source" "$framework_json" | tr -d '\r')"
  [[ -n "$framework_source" ]] || fail "automation-framework dependency source is missing in $pyproject_path."

  if [[ "$framework_kind" == "git" ]]; then
    local framework_repo_url framework_ref framework_slug framework_archive framework_root
    framework_repo_url="$(python_json_get "repo_url" "$framework_json" | tr -d '\r')"
    framework_ref="$(python_json_get "ref" "$framework_json" | tr -d '\r')"
    if framework_slug="$(parse_github_slug "$framework_repo_url" 2>/dev/null)"; then
      framework_archive="$TMP_ROOT/framework-archive.zip"
      LAST_FRAMEWORK_ARCHIVE_URL="https://api.github.com/repos/$framework_slug/zipball/$framework_ref"
      log "Downloading automation-framework pinned in pyproject.toml"
      download_file "$LAST_FRAMEWORK_ARCHIVE_URL" "$framework_archive"
      framework_root="$(extract_archive "$framework_archive" "$TMP_ROOT/framework-extracted")"
      log "Installing automation-framework from downloaded source"
      "$UV_BIN" pip install --python "$venv_python" "$framework_root"
      return 0
    fi
  fi

  log "Installing automation-framework directly from pyproject.toml"
  "$UV_BIN" pip install --python "$venv_python" "$framework_source"
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

write_browser_profiles_if_missing() {
  local install_dir="$1"
  local browser_profiles="$install_dir/config/browser_profiles.json"
  mkdir -p "$install_dir/config"

  if [[ -f "$browser_profiles" ]]; then
    log "Reusing existing browser profiles: $browser_profiles"
    return 0
  fi

  cat > "$browser_profiles" <<'JSON'
{
  "local-chrome": {
    "provider": "chrome_cdp",
    "profile_id": "local-chrome",
    "metadata": {
      "debug_http": "http://127.0.0.1:9222"
    }
  }
}
JSON
}

merge_key_value_file() {
  local file_path="$1"
  shift

  local args=(
    "$OPENCLAW_DEPLOY_UTILS"
    merge-key-value-file
    --path
    "$file_path"
  )

  local entry
  for entry in "$@"; do
    args+=(--managed "$entry")
  done

  "$PYTHON_BIN" "${args[@]}"
}

remove_key_value_file() {
  local file_path="$1"
  shift

  local args=(
    "$OPENCLAW_DEPLOY_UTILS"
    remove-key-value-file
    --path
    "$file_path"
  )

  local key
  for key in "$@"; do
    args+=(--key "$key")
  done

  "$PYTHON_BIN" "${args[@]}"
}

remove_skill_runtime_config_keys() {
  local skill_env="$1"

  remove_key_value_file \
    "$skill_env" \
    "BROWSER_PROFILE_REF" \
    "BROWSER_PROVIDER_NAME" \
    "BROWSER_PROFILE_ID" \
    "BROWSER_WORKSPACE_ID" \
    "BROWSER_PROFILES_FILE" \
    "DEFAULT_PROFILE_REF" \
    "MUJITASK_BROWSER_PROFILE_REF" \
    "MUJITASK_DB_URL" \
    "MUJITASK_FACT_DB_URL" \
    "MUJITASK_ARTIFACT_ROOT" \
    "MUJITASK_ARTIFACT_BUCKET" \
    "MUJITASK_ARTIFACT_STORE_PROVIDER" \
    "MUJITASK_MINIO_BUCKET" \
    "EXECUTION_CONTROL_DB_URL" \
    "EXECUTION_CONTROL_FACT_DB_URL" \
    "EXECUTION_CONTROL_ARTIFACT_ROOT" \
    "EXECUTION_CONTROL_ARTIFACT_BUCKET" \
    "EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER" \
    "EXECUTION_CONTROL_ARTIFACT_OBJECT_PREFIX" \
    "EXECUTION_CONTROL_MINIO_ENDPOINT" \
    "EXECUTION_CONTROL_MINIO_ACCESS_KEY" \
    "EXECUTION_CONTROL_MINIO_SECRET_KEY" \
    "EXECUTION_CONTROL_MINIO_REGION" \
    "EXECUTION_CONTROL_MINIO_SECURE" \
    "EXECUTION_CONTROL_MINIO_CREATE_BUCKET" \
    "EXECUTION_CONTROL_SYNC_REFERENCED_FILES" \
    "EXECUTION_CONTROL_REQUESTED_BY" \
    "BUSINESS_EXECUTION_CONTROL_DB_URL" \
    "BUSINESS_EXECUTION_CONTROL_FACT_DB_URL" \
    "BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT" \
    "BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET" \
    "BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER" \
    "BUSINESS_EXECUTION_CONTROL_ARTIFACT_OBJECT_PREFIX" \
    "BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT" \
    "BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY" \
    "BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY" \
    "BUSINESS_EXECUTION_CONTROL_MINIO_REGION" \
    "BUSINESS_EXECUTION_CONTROL_MINIO_SECURE" \
    "BUSINESS_EXECUTION_CONTROL_MINIO_CREATE_BUCKET" \
    "BUSINESS_EXECUTION_CONTROL_SYNC_REFERENCED_FILES" \
    "BUSINESS_EXECUTION_CONTROL_REQUESTED_BY" \
    "TK_FACT_DB_URL"
}

seed_key_value_file_from_example() {
  local file_path="$1"
  local example_path="$2"

  if [[ -f "$file_path" || ! -f "$example_path" ]]; then
    return 0
  fi

  mkdir -p "$(dirname "$file_path")"
  cp "$example_path" "$file_path"
}

write_skill_local_env() {
  local skill_dir="$1"
  local install_dir="$2"
  local feishu_base_url="$3"
  local tk_selection_table_id="$4"
  local tk_selection_view_id="$5"
  local tk_competitor_table_id="$6"
  local tk_competitor_view_id="$7"
  local tk_influencer_pool_table_id="$8"
  local tk_influencer_pool_view_id="$9"
  local tk_influencer_outreach_table_id="${10}"
  local tk_influencer_outreach_view_id="${11}"
  local tk_hot_video_table_id="${12}"
  local tk_hot_video_view_id="${13}"
  local token="${14}"
  local fastmoss_phone="${15}"
  local fastmoss_password="${16}"
  local notification_channel_code="${17}"
  local openclaw_agent_id="${18}"
  local openclaw_state_dir="${19}"

  seed_key_value_file_from_example "$skill_dir/skill.local.env" "$skill_dir/skill.local.env.example"
  remove_skill_runtime_config_keys "$skill_dir/skill.local.env"

  merge_key_value_file \
    "$skill_dir/skill.local.env" \
    "INSTALL_DIR=$(quote_env_value "$install_dir")" \
    "MUJITASK_FEISHU_BASE_URL=$(quote_env_value "$feishu_base_url")" \
    "MUJITASK_FEISHU_TK_SELECTION_TABLE_ID=$(quote_env_value "$tk_selection_table_id")" \
    "MUJITASK_FEISHU_TK_SELECTION_VIEW_ID=$(quote_env_value "$tk_selection_view_id")" \
    "MUJITASK_FEISHU_TK_COMPETITOR_TABLE_ID=$(quote_env_value "$tk_competitor_table_id")" \
    "MUJITASK_FEISHU_TK_COMPETITOR_VIEW_ID=$(quote_env_value "$tk_competitor_view_id")" \
    "MUJITASK_FEISHU_TK_INFLUENCER_POOL_TABLE_ID=$(quote_env_value "$tk_influencer_pool_table_id")" \
    "MUJITASK_FEISHU_TK_INFLUENCER_POOL_VIEW_ID=$(quote_env_value "$tk_influencer_pool_view_id")" \
    "MUJITASK_FEISHU_TK_INFLUENCER_OUTREACH_TABLE_ID=$(quote_env_value "$tk_influencer_outreach_table_id")" \
    "MUJITASK_FEISHU_TK_INFLUENCER_OUTREACH_VIEW_ID=$(quote_env_value "$tk_influencer_outreach_view_id")" \
    "MUJITASK_FEISHU_TK_HOT_VIDEO_TABLE_ID=$(quote_env_value "$tk_hot_video_table_id")" \
    "MUJITASK_FEISHU_TK_HOT_VIDEO_VIEW_ID=$(quote_env_value "$tk_hot_video_view_id")" \
    "MUJITASK_FEISHU_ACCESS_TOKEN=$(quote_env_value "$token")" \
    "FASTMOSS_PHONE=$(quote_env_value "$fastmoss_phone")" \
    "FASTMOSS_PASSWORD=$(quote_env_value "$fastmoss_password")" \
    "NOTIFICATION_CHANNEL_CODE=$(quote_env_value "$notification_channel_code")" \
    "OPENCLAW_AGENT_ID=$(quote_env_value "$openclaw_agent_id")" \
    "OPENCLAW_STATE_DIR=$(quote_env_value "$openclaw_state_dir")"
}

write_executor_local_env() {
  local install_dir="$1"
  local db_url="$2"
  local artifact_root="$3"
  local artifact_bucket="$4"
  local artifact_store_provider="$5"
  local artifact_object_prefix="$6"
  local minio_endpoint="$7"
  local minio_access_key="$8"
  local minio_secret_key="$9"
  local minio_region="${10}"
  local minio_secure="${11}"
  local minio_create_bucket="${12}"
  local sync_referenced_files="${13}"
  local requested_by="${14}"
  local token="${15}"
  local browser_profile_ref="${16}"
  local fastmoss_phone="${17}"
  local fastmoss_password="${18}"
  local notification_channel_code="${19}"

  local executor_env="$install_dir/scripts/execution_control/executor.local.env"
  local executor_example="$install_dir/scripts/execution_control/executor.local.env.example"

  seed_key_value_file_from_example "$executor_env" "$executor_example"

  merge_key_value_file \
    "$executor_env" \
    "BUSINESS_EXECUTION_CONTROL_DB_URL=$(quote_env_value "$db_url")" \
    "BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT=$(quote_env_value "$artifact_root")" \
    "BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET=$(quote_env_value "$artifact_bucket")" \
    "BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER=$(quote_env_value "$artifact_store_provider")" \
    "BUSINESS_EXECUTION_CONTROL_ARTIFACT_OBJECT_PREFIX=$(quote_env_value "$artifact_object_prefix")" \
    "BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT=$(quote_env_value "$minio_endpoint")" \
    "BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY=$(quote_env_value "$minio_access_key")" \
    "BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY=$(quote_env_value "$minio_secret_key")" \
    "BUSINESS_EXECUTION_CONTROL_MINIO_REGION=$(quote_env_value "$minio_region")" \
    "BUSINESS_EXECUTION_CONTROL_MINIO_SECURE=$(quote_env_value "$minio_secure")" \
    "BUSINESS_EXECUTION_CONTROL_MINIO_CREATE_BUCKET=$(quote_env_value "$minio_create_bucket")" \
    "BUSINESS_EXECUTION_CONTROL_SYNC_REFERENCED_FILES=$(quote_env_value "$sync_referenced_files")" \
    "BUSINESS_EXECUTION_CONTROL_REQUESTED_BY=$(quote_env_value "$requested_by")" \
    "MUJITASK_FEISHU_ACCESS_TOKEN=$(quote_env_value "$token")" \
    "BROWSER_PROFILE_REF=$(quote_env_value "$browser_profile_ref")" \
    "FASTMOSS_PHONE=$(quote_env_value "$fastmoss_phone")" \
    "FASTMOSS_PASSWORD=$(quote_env_value "$fastmoss_password")" \
    "NOTIFICATION_CHANNEL_CODE=$(quote_env_value "$notification_channel_code")"
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

read_kv_value() {
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

write_deploy_state() {
  local install_dir="$1"
  local repo_url="$2"
  local resolved_ref="$3"
  local repo_archive_url="$4"
  local framework_archive_url="$5"

  local deploy_dir="$install_dir/runtime/deployment"
  mkdir -p "$deploy_dir"

  "$PYTHON_BIN" "$OPENCLAW_DEPLOY_UTILS" write-deploy-state \
    --path "$deploy_dir/openclaw-deploy.env" \
    --repo-url "$repo_url" \
    --resolved-ref "$resolved_ref" \
    --repo-archive-url "$repo_archive_url" \
    --framework-archive-url "$framework_archive_url" \
    --install-layout-version "1" \
    --update-supported "1"
}

sync_install_tree() {
  local source_dir="$1"
  local target_dir="$2"

  "$PYTHON_BIN" "$OPENCLAW_DEPLOY_UTILS" sync-install-tree \
    --source "$source_dir" \
    --target "$target_dir" \
    --preserve ".venv" \
    --preserve "runtime" \
    --preserve ".env" \
    --preserve "config/browser_profiles.json" \
    --preserve "scripts/execution_control/executor.local.env"
}

deploy_state_supports_update() {
  local deploy_state_path="$1"
  "$PYTHON_BIN" "$OPENCLAW_DEPLOY_UTILS" check-update-support --path "$deploy_state_path"
}

directory_has_entries() {
  local directory="$1"
  [[ -d "$directory" ]] || return 1
  local children=()
  shopt -s dotglob nullglob
  children=("$directory"/*)
  shopt -u dotglob nullglob
  ((${#children[@]} > 0))
}

smoke_check() {
  local install_dir="$1"
  local target_skill_dir="$2"
  local executor_env_path="$3"
  local cli_bin="$install_dir/.venv/bin/automation-business-scaffold-run"
  local python_bin="$install_dir/.venv/bin/python"
  local tasks_json="$TMP_ROOT/tasks.json"

  [[ -x "$cli_bin" ]] || fail "Smoke check failed: $cli_bin is missing."
  [[ -x "$python_bin" ]] || fail "Smoke check failed: $python_bin is missing."

  "$cli_bin" list-tasks > "$tasks_json"

  "$python_bin" - "$tasks_json" <<'PY'
import json
import sys

required = {
    "refresh_competitor_row_by_url",
    "refresh_current_competitor_table",
    "search_keyword_competitor_products",
    "search_keyword_selection_products",
    "sync_tk_influencer_pool",
    "tiktok_fastmoss_product_ingest",
    "tiktok_influencer_outreach_sync",
}
with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)

names = {item.get("name") for item in payload.get("tasks", []) if isinstance(item, dict)}
missing = sorted(required - names)
if missing:
    raise SystemExit(f"Missing tasks: {', '.join(missing)}")
PY

  local required_files=(
    "SKILL.md"
    "skill.local.env"
    "skill.local.env.example"
    "run_refresh_current_competitor_table_step.sh"
    "run_competitor_row_by_url_step.sh"
    "run_product_url_complete_step.sh"
    "run_keyword_search_step.sh"
    "run_influencer_pool_sync_step.sh"
    "run_skill_step.py"
    "lightweight_submit.py"
  )

  local file_name
  for file_name in "${required_files[@]}"; do
    [[ -f "$target_skill_dir/$file_name" ]] || fail "Smoke check failed: $target_skill_dir/$file_name is missing."
  done

  check_skill_frontmatter "$target_skill_dir/SKILL.md" \
    || fail "Smoke check failed: $target_skill_dir/SKILL.md frontmatter is invalid."
  validate_project_node_dependencies "$install_dir" "$python_bin" \
    || fail "Smoke check failed: FastMoss visualization renderer dependencies are unavailable."

  local required_runtime_files=(
    "$install_dir/scripts/execution_control/install_launch_agents.sh"
    "$install_dir/scripts/execution_control/run_launchd_agent.sh"
    "$install_dir/config/deployment/launchd/com.happyzhao.mujitask.executor-daemon.plist.template"
    "$install_dir/config/deployment/launchd/com.happyzhao.mujitask.api-worker.plist.template"
    "$install_dir/config/deployment/launchd/com.happyzhao.mujitask.browser-runloop.plist.template"
    "$install_dir/config/deployment/launchd/com.happyzhao.mujitask.outbox-dispatcher.plist.template"
    "$executor_env_path"
  )

  for file_name in "${required_runtime_files[@]}"; do
    [[ -f "$file_name" ]] || fail "Smoke check failed: $file_name is missing."
  done

  local launchd_uid
  launchd_uid="$(id -u)"
  local launchd_label
  for launchd_label in \
    "com.happyzhao.mujitask.executor-daemon" \
    "com.happyzhao.mujitask.api-worker" \
    "com.happyzhao.mujitask.browser-runloop" \
    "com.happyzhao.mujitask.outbox-dispatcher" \
    "com.happyzhao.mujitask.watchdog"; do
    if launchctl print "gui/${launchd_uid}/${launchd_label}" >/dev/null 2>&1; then
      :
    else
      fail "Smoke check failed: launchd service ${launchd_label} is not loaded."
    fi
  done

  set -a
  # shellcheck disable=SC1090
  source "$executor_env_path"
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
    "api_worker_job",
    "resource_lease",
    "notification_outbox",
    "artifact_object",
    "fastmoss_session_cookie_cache",
    "tk_products",
    "tk_creators",
    "tk_videos",
    "tk_video_product_relations",
    "tk_video_metric_snapshots",
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
}
