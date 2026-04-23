#!/usr/bin/env bash

set -euo pipefail

OPENCLAW_LOG_PREFIX="deploy-openclaw"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_SELF="${BASH_SOURCE[0]}"

extract_embedded_block() {
  local marker="$1"
  awk -v marker="$marker" '
    index($0, marker) && index($0, ": <<") == 1 {capture=1; next}
    capture && $0 == marker {exit}
    capture {print}
  ' "$SCRIPT_SELF"
}

load_openclaw_common() {
  local common_path="$SCRIPT_DIR/openclaw_deploy_common.sh"
  local utils_path="$SCRIPT_DIR/openclaw_deploy_utils.py"

  if [[ -f "$common_path" && -f "$utils_path" ]]; then
    # shellcheck source=./openclaw_deploy_common.sh
    source "$common_path"
    return 0
  fi

  TMP_ROOT="${TMP_ROOT:-$(mktemp -d)}"
  common_path="$TMP_ROOT/openclaw_deploy_common.sh"
  utils_path="$TMP_ROOT/openclaw_deploy_utils.py"

  extract_embedded_block "__OPENCLAW_DEPLOY_COMMON__" > "$common_path"
  extract_embedded_block "__OPENCLAW_DEPLOY_UTILS__" > "$utils_path"
  chmod +x "$utils_path"

  # shellcheck source=/dev/null
  source "$common_path"
}

load_openclaw_common

main() {
  ensure_uv
  ensure_python_311

  local openclaw_skills_dir
  openclaw_skills_dir="$(resolve_openclaw_skills_dir)"
  local existing_skill_env="$openclaw_skills_dir/mujitask-tiktok-feishu-sync/skill.local.env"
  local existing_install_dir=""
  existing_install_dir="$(read_kv_value "$existing_skill_env" "INSTALL_DIR" 2>/dev/null || true)"
  local existing_browser_profile_ref=""
  local existing_fastmoss_phone=""
  local existing_fastmoss_password=""
  local existing_db_url=""
  local existing_artifact_root=""
  local existing_artifact_bucket=""
  local existing_notification_channel_code=""
  local existing_openclaw_agent_id=""
  local existing_openclaw_state_dir=""

  local repo_url="" tag="" install_dir="" table_url="" token="" archive_url="" github_slug="" resolved_ref="" github_token_input=""
  local default_install_dir="$HOME/apps/mujitask"
  if [[ -n "$existing_install_dir" ]]; then
    default_install_dir="$existing_install_dir"
  fi
  if [[ -f "$existing_skill_env" ]]; then
    existing_browser_profile_ref="$(read_kv_value "$existing_skill_env" "BROWSER_PROFILE_REF" 2>/dev/null || true)"
    existing_fastmoss_phone="$(read_kv_value "$existing_skill_env" "FASTMOSS_PHONE" 2>/dev/null || true)"
    existing_fastmoss_password="$(read_kv_value "$existing_skill_env" "FASTMOSS_PASSWORD" 2>/dev/null || true)"
    existing_db_url="$(read_kv_value "$existing_skill_env" "EXECUTION_CONTROL_DB_URL" 2>/dev/null || true)"
    existing_artifact_root="$(read_kv_value "$existing_skill_env" "EXECUTION_CONTROL_ARTIFACT_ROOT" 2>/dev/null || true)"
    existing_artifact_bucket="$(read_kv_value "$existing_skill_env" "EXECUTION_CONTROL_ARTIFACT_BUCKET" 2>/dev/null || true)"
    existing_notification_channel_code="$(read_kv_value "$existing_skill_env" "NOTIFICATION_CHANNEL_CODE" 2>/dev/null || true)"
    existing_openclaw_agent_id="$(read_kv_value "$existing_skill_env" "OPENCLAW_AGENT_ID" 2>/dev/null || true)"
    existing_openclaw_state_dir="$(read_kv_value "$existing_skill_env" "OPENCLAW_STATE_DIR" 2>/dev/null || true)"
  fi

  if [[ -z "$GITHUB_AUTH_TOKEN" ]]; then
    github_token_input="$(prompt_secret_optional "GitHub PAT for private GitHub repos (optional, press Enter to skip)")"
    if [[ -n "$github_token_input" ]]; then
      GITHUB_AUTH_TOKEN="$github_token_input"
    fi
  fi

  tag="$(prompt_optional "Tag (leave blank to auto-resolve latest)" "")"
  install_dir="$(prompt "Install directory" "$default_install_dir")"

  local deploy_state_path="$install_dir/runtime/deployment/openclaw-deploy.env"
  if [[ -f "$deploy_state_path" ]] && deploy_state_supports_update "$deploy_state_path"; then
    fail "Managed install already exists at $install_dir. Use update-openclaw.sh instead."
  fi
  if [[ -e "$install_dir" ]] && directory_has_entries "$install_dir"; then
    fail "Install directory already exists and is not empty: $install_dir. Choose a new directory or clear it manually."
  fi
  mkdir -p "$install_dir"

  if [[ -f "$existing_skill_env" ]]; then
    table_url="$(read_kv_value "$existing_skill_env" "TABLE_URL" 2>/dev/null || true)"
    token="$(read_kv_value "$existing_skill_env" "FEISHU_ACCESS_TOKEN" 2>/dev/null || true)"
  fi

  local existing_executor_env=""
  if [[ -n "$existing_install_dir" ]]; then
    existing_executor_env="$existing_install_dir/scripts/execution_control/executor.local.env"
    if [[ -f "$existing_executor_env" ]]; then
      [[ -n "$existing_db_url" ]] || existing_db_url="$(read_kv_value "$existing_executor_env" "BUSINESS_EXECUTION_CONTROL_DB_URL" 2>/dev/null || true)"
      [[ -n "$existing_artifact_root" ]] || existing_artifact_root="$(read_kv_value "$existing_executor_env" "BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT" 2>/dev/null || true)"
      [[ -n "$existing_artifact_bucket" ]] || existing_artifact_bucket="$(read_kv_value "$existing_executor_env" "BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET" 2>/dev/null || true)"
      [[ -n "$existing_notification_channel_code" ]] || existing_notification_channel_code="$(read_kv_value "$existing_executor_env" "NOTIFICATION_CHANNEL_CODE" 2>/dev/null || true)"
    fi
  fi

  local browser_profile_ref="" fastmoss_phone="" fastmoss_password=""
  local db_url="" artifact_root="" artifact_bucket=""
  local artifact_store_provider="" artifact_object_prefix=""
  local minio_endpoint="" minio_access_key="" minio_secret_key="" minio_region=""
  local minio_secure="" minio_create_bucket="" sync_referenced_files=""
  local requested_by="" notification_channel_code="" openclaw_agent_id="" openclaw_state_dir=""

  repo_url="$(prompt "Repo URL")"

  if [[ -n "$table_url" ]]; then
    log "Reusing existing Feishu table URL from $existing_skill_env"
  else
    table_url="$(prompt "Feishu table URL")"
  fi

  if [[ -n "$token" ]]; then
    log "Reusing existing Feishu access token from $existing_skill_env"
  else
    token="$(prompt_secret "Feishu access token")"
  fi

  browser_profile_ref="$(prompt "Browser profile ref" "${existing_browser_profile_ref:-roxy-tiktok}")"
  if [[ -n "$existing_fastmoss_phone" ]]; then
    log "Reusing existing FastMoss phone from $existing_skill_env"
    fastmoss_phone="$existing_fastmoss_phone"
  else
    fastmoss_phone="$(prompt "FastMoss phone")"
  fi
  if [[ -n "$existing_fastmoss_password" ]]; then
    log "Reusing existing FastMoss password from $existing_skill_env"
    fastmoss_password="$existing_fastmoss_password"
  else
    fastmoss_password="$(prompt_secret "FastMoss password")"
  fi

  db_url="$(prompt "Execution control DB URL" "${existing_db_url:-postgresql+psycopg://postgres:postgres@127.0.0.1:5432/automation_business_scaffold}")"
  artifact_root="$(prompt "Artifact root" "${existing_artifact_root:-$install_dir/runtime/execution_control/object_store}")"
  artifact_bucket="$(prompt "Artifact bucket" "${existing_artifact_bucket:-automation-business-scaffold}")"
  artifact_store_provider="$(prompt "Artifact store provider" "minio")"
  artifact_object_prefix="$(prompt_optional "Artifact object prefix" "phase2/local")"
  minio_endpoint="$(prompt_optional "MinIO endpoint" "127.0.0.1:9000")"
  minio_access_key="$(prompt_optional "MinIO access key" "minioadmin")"
  minio_secret_key="$(prompt_secret_optional "MinIO secret key (optional, press Enter to use default)")"
  if [[ -z "$minio_secret_key" ]]; then
    minio_secret_key="minioadmin"
  fi
  minio_region="$(prompt_optional "MinIO region (optional)" "")"
  minio_secure="$(prompt_optional "MinIO secure (true/false)" "false")"
  minio_create_bucket="$(prompt_optional "MinIO create bucket automatically (true/false)" "true")"
  sync_referenced_files="$(prompt_optional "Sync referenced files to object storage (true/false)" "true")"
  requested_by="$(prompt_optional "Requested by marker" "openclaw-skill")"
  notification_channel_code="$(prompt_optional "Notification channel code" "${existing_notification_channel_code:-feishu_bot_api}")"
  openclaw_agent_id="$(prompt_optional "OpenClaw agent id" "${existing_openclaw_agent_id:-tiktok-ops}")"
  openclaw_state_dir="$(prompt_optional "OpenClaw state dir" "${existing_openclaw_state_dir:-$HOME/.openclaw}")"

  local repo_archive="$TMP_ROOT/project-archive"
  local project_root

  if github_slug="$(parse_github_slug "$repo_url" 2>/dev/null)"; then
    resolved_ref="$tag"
    if [[ -z "$resolved_ref" ]]; then
      log "Resolving latest release/tag for $github_slug"
      resolved_ref="$(resolve_latest_github_ref "$github_slug")"
    fi
    archive_url="https://api.github.com/repos/$github_slug/zipball/$resolved_ref"
    repo_archive="$repo_archive.zip"
  else
    archive_url="$(prompt "Archive URL for the repository source package")"
    local lowered_archive
    lowered_archive="$(to_lower "$archive_url")"
    if [[ "$lowered_archive" == *.tar.gz || "$lowered_archive" == *.tgz || "$lowered_archive" == *.tar ]]; then
      repo_archive="$repo_archive.tar.gz"
    else
      repo_archive="$repo_archive.zip"
    fi
    resolved_ref="${tag:-custom-archive}"
  fi

  log "Downloading project archive"
  download_file "$archive_url" "$repo_archive"

  local extracted_project_dir="$TMP_ROOT/project-extracted"
  project_root="$(extract_archive "$repo_archive" "$extracted_project_dir")"

  cp -R "$project_root"/. "$install_dir"/

  local pyproject_path="$install_dir/pyproject.toml"
  [[ -f "$pyproject_path" ]] || fail "Missing $pyproject_path after extraction."

  log "Creating project virtual environment"
  "$UV_BIN" venv --python 3.11 "$install_dir/.venv"

  local venv_python="$install_dir/.venv/bin/python"
  [[ -x "$venv_python" ]] || fail "Virtual environment python was not created."

  install_framework_from_pyproject "$pyproject_path" "$venv_python"

  local project_deps=()
  local dep
  while IFS= read -r dep; do
    [[ -n "$dep" ]] && project_deps+=("$dep")
  done < <(read_project_dependencies "$install_dir/pyproject.toml")
  if ((${#project_deps[@]} > 0)); then
    log "Installing project runtime dependencies"
    "$UV_BIN" pip install --python "$venv_python" "${project_deps[@]}"
  fi

  log "Installing project package"
  "$UV_BIN" pip install --python "$venv_python" -e "$install_dir" --no-deps

  log "Installing Playwright Chromium"
  "$venv_python" -m playwright install chromium

  mkdir -p \
    "$install_dir/runtime/cli_runs" \
    "$install_dir/runtime/artifacts" \
    "$install_dir/runtime/downloads" \
    "$install_dir/runtime/phase1_daemons" \
    "$install_dir/runtime/execution_control"
  write_browser_profiles_if_missing "$install_dir"

  local chrome_bin
  chrome_bin="$(detect_chrome_bin || true)"
  if [[ -z "$chrome_bin" ]]; then
    warn "Google Chrome was not found."
    warn "Install Chrome and rerun this deployment script."
    exit 2
  fi

  local source_skill_dir="$install_dir/skills/mujitask-tiktok-feishu-sync"
  local target_skill_dir="$openclaw_skills_dir/mujitask-tiktok-feishu-sync"
  local previous_skill_env="$TMP_ROOT/previous-skill.local.env"

  [[ -d "$source_skill_dir" ]] || fail "Missing skill bundle at $source_skill_dir."
  if [[ -f "$existing_skill_env" ]]; then
    cp "$existing_skill_env" "$previous_skill_env"
  fi

  replace_target_dir "$target_skill_dir"
  cp -R "$source_skill_dir"/. "$target_skill_dir"/
  if [[ -f "$previous_skill_env" ]]; then
    cp "$previous_skill_env" "$target_skill_dir/skill.local.env"
  fi
  write_skill_local_env \
    "$target_skill_dir" \
    "$install_dir" \
    "$table_url" \
    "$token" \
    "$browser_profile_ref" \
    "$fastmoss_phone" \
    "$fastmoss_password" \
    "$db_url" \
    "$artifact_root" \
    "$artifact_bucket" \
    "$requested_by" \
    "$notification_channel_code" \
    "$openclaw_agent_id" \
    "$openclaw_state_dir"
  write_executor_local_env \
    "$install_dir" \
    "$db_url" \
    "$artifact_root" \
    "$artifact_bucket" \
    "$artifact_store_provider" \
    "$artifact_object_prefix" \
    "$minio_endpoint" \
    "$minio_access_key" \
    "$minio_secret_key" \
    "$minio_region" \
    "$minio_secure" \
    "$minio_create_bucket" \
    "$sync_referenced_files" \
    "$requested_by" \
    "$token" \
    "$browser_profile_ref" \
    "$fastmoss_phone" \
    "$fastmoss_password" \
    "$notification_channel_code"
  write_deploy_state "$install_dir" "$repo_url" "$resolved_ref" "${archive_url:-}" "${LAST_FRAMEWORK_ARCHIVE_URL:-}"
  log "Installing launchd agents"
  bash "$install_dir/scripts/execution_control/install_launch_agents.sh"

  smoke_check "$install_dir" "$target_skill_dir" "$install_dir/scripts/execution_control/executor.local.env"

  log "Deployment completed."
  log "Installed ref: $resolved_ref"
  log "Project directory: $install_dir"
  log "OpenClaw skill directory: $target_skill_dir"
  log "Chrome binary: $chrome_bin"
}

main "$@"

: <<'__OPENCLAW_DEPLOY_COMMON__'
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

write_skill_local_env() {
  local skill_dir="$1"
  local install_dir="$2"
  local table_url="$3"
  local token="$4"

  merge_key_value_file \
    "$skill_dir/skill.local.env" \
    "INSTALL_DIR=$install_dir" \
    "TABLE_URL=$table_url" \
    "FEISHU_ACCESS_TOKEN=$token"
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
    --preserve "config/browser_profiles.json"
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
  local cli_bin="$install_dir/.venv/bin/automation-business-scaffold-run"
  local tasks_json="$TMP_ROOT/tasks.json"

  [[ -x "$cli_bin" ]] || fail "Smoke check failed: $cli_bin is missing."

  "$cli_bin" list-tasks > "$tasks_json"

  "$PYTHON_BIN" - "$tasks_json" <<'PY'
import json
import sys

required = {
    "tiktok_product_link_cleanup",
    "feishu_pending_rows_scan",
    "feishu_single_row_update",
    "feishu_seed_row_insert",
    "fastmoss_keyword_candidate_discovery",
    "fastmoss_login_check",
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
    "run_cleanup_step.sh"
    "run_pending_rows_step.sh"
    "run_single_row_update_step.sh"
    "run_keyword_candidate_step.sh"
    "run_insert_seed_row_step.sh"
    "run_fastmoss_login_check_step.sh"
    "start_browser_cdp.sh"
    "start_browser_cdp.ps1"
  )

  local file_name
  for file_name in "${required_files[@]}"; do
    [[ -f "$target_skill_dir/$file_name" ]] || fail "Smoke check failed: $target_skill_dir/$file_name is missing."
  done

  check_skill_frontmatter "$target_skill_dir/SKILL.md" \
    || fail "Smoke check failed: $target_skill_dir/SKILL.md frontmatter is invalid."
}
__OPENCLAW_DEPLOY_COMMON__

: <<'__OPENCLAW_DEPLOY_UTILS__'
#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import shutil
import tomllib
from pathlib import Path


def _normalize_env_entry(value: str) -> str:
    normalized = value.strip().lstrip("\ufeff")
    if normalized.startswith("export "):
        normalized = normalized[len("export ") :].strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {'"', "'"}:
        normalized = normalized[1:-1]
    return normalized


def _parse_key_value_line(raw_line: str) -> tuple[str, str] | None:
    stripped = raw_line.strip()
    if not stripped or stripped.startswith("#") or "=" not in raw_line:
        return None
    key, value = raw_line.split("=", 1)
    normalized_key = _normalize_env_entry(key)
    if not normalized_key:
        return None
    return normalized_key, _normalize_env_entry(value)


def merge_key_value_file(path: Path, managed: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    seen_keys: set[str] = set()
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

    output_lines: list[str] = []
    for raw_line in lines:
        parsed = _parse_key_value_line(raw_line)
        if parsed is None:
            output_lines.append(raw_line)
            continue

        key, _ = parsed
        if key in managed and key not in seen_keys:
            output_lines.append(f"{key}={managed[key]}\n")
            seen_keys.add(key)
            continue

        output_lines.append(raw_line)

    for key, value in managed.items():
        if key in seen_keys:
            continue
        output_lines.append(f"{key}={value}\n")

    text = "".join(output_lines)
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")


def write_deploy_state_file(
    path: Path,
    *,
    repo_url: str,
    resolved_ref: str,
    repo_archive_url: str,
    framework_archive_url: str,
    install_layout_version: str,
    update_supported: str,
) -> None:
    managed = {
        "REPO_URL": repo_url,
        "LAST_RESOLVED_REF": resolved_ref,
        "REPO_ARCHIVE_URL": repo_archive_url,
        "FRAMEWORK_ARCHIVE_URL": framework_archive_url,
        "INSTALL_LAYOUT_VERSION": install_layout_version,
        "UPDATE_SUPPORTED": update_supported,
    }
    merge_key_value_file(path, managed)


def read_framework_dependency(path: Path) -> dict[str, str]:
    with open(path, "rb") as handle:
        data = tomllib.load(handle)

    dependencies = data.get("project", {}).get("dependencies", [])
    matches = [dep for dep in dependencies if dep.startswith("automation-framework @ ")]
    if len(matches) != 1:
        raise ValueError(
            "pyproject.toml must declare exactly one 'automation-framework @ ...' dependency."
        )

    dependency = matches[0]
    source = dependency.split(" @ ", 1)[1].strip()
    result = {
        "dependency": dependency,
        "source": source,
        "kind": "direct",
    }
    if source.startswith("git+"):
        git_source = source[len("git+") :]
        base, _, fragment = git_source.partition("#")
        repo_url, sep, ref = base.rpartition("@")
        if sep and repo_url and ref:
            result["kind"] = "git"
            result["repo_url"] = repo_url
            result["ref"] = ref
        if fragment:
            result["fragment"] = fragment
    return result


def deploy_state_supports_update(path: Path) -> bool:
    if not path.exists():
        return False
    data = load_key_value_file(path)
    return (
        data.get("INSTALL_LAYOUT_VERSION", "").strip() == "1"
        and data.get("UPDATE_SUPPORTED", "").strip() == "1"
    )


def load_key_value_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_key_value_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        if key not in data:
            data[key] = value
    return data


def _is_preserved_path(rel_path: Path, preserved: set[Path]) -> bool:
    return any(rel_path == candidate or candidate in rel_path.parents for candidate in preserved)


def _is_preserved_ancestor(rel_path: Path, preserved: set[Path]) -> bool:
    return any(rel_path in candidate.parents for candidate in preserved)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _clean_target_dir(target_root: Path, current_dir: Path, preserved: set[Path]) -> None:
    for child in current_dir.iterdir():
        rel_path = child.relative_to(target_root)
        if _is_preserved_path(rel_path, preserved):
            continue
        if _is_preserved_ancestor(rel_path, preserved) and child.is_dir():
            _clean_target_dir(target_root, child, preserved)
            continue
        _remove_path(child)


def _copy_tree(source_root: Path, current_source: Path, target_root: Path, preserved: set[Path]) -> None:
    for child in current_source.iterdir():
        rel_path = child.relative_to(source_root)
        if _is_preserved_path(rel_path, preserved):
            continue

        target_path = target_root / rel_path
        if child.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
            _copy_tree(source_root, child, target_root, preserved)
            continue

        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(child, target_path)


def sync_install_tree(source_dir: Path, target_dir: Path, preserve_paths: list[str]) -> None:
    source_dir = source_dir.resolve()
    target_dir = target_dir.resolve()
    preserved = {Path(item) for item in preserve_paths}

    target_dir.mkdir(parents=True, exist_ok=True)
    _clean_target_dir(target_dir, target_dir, preserved)
    _copy_tree(source_dir, source_dir, target_dir, preserved)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Helpers for OpenClaw deploy/update scripts.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    merge_parser = subparsers.add_parser("merge-key-value-file")
    merge_parser.add_argument("--path", required=True)
    merge_parser.add_argument("--managed", action="append", default=[], metavar="KEY=VALUE")

    deploy_state_parser = subparsers.add_parser("write-deploy-state")
    deploy_state_parser.add_argument("--path", required=True)
    deploy_state_parser.add_argument("--repo-url", required=True)
    deploy_state_parser.add_argument("--resolved-ref", required=True)
    deploy_state_parser.add_argument("--repo-archive-url", default="")
    deploy_state_parser.add_argument("--framework-archive-url", default="")
    deploy_state_parser.add_argument("--install-layout-version", default="1")
    deploy_state_parser.add_argument("--update-supported", default="1")

    framework_parser = subparsers.add_parser("read-framework-dependency")
    framework_parser.add_argument("--path", required=True)

    support_parser = subparsers.add_parser("check-update-support")
    support_parser.add_argument("--path", required=True)

    sync_parser = subparsers.add_parser("sync-install-tree")
    sync_parser.add_argument("--source", required=True)
    sync_parser.add_argument("--target", required=True)
    sync_parser.add_argument("--preserve", action="append", default=[])
    return parser


def _parse_managed_pairs(items: list[str]) -> dict[str, str]:
    managed: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Invalid managed entry: {item}")
        key, value = item.split("=", 1)
        key = _normalize_env_entry(key)
        if not key:
            raise ValueError(f"Managed entry key cannot be empty: {item}")
        managed[key] = value
    return managed


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "merge-key-value-file":
        merge_key_value_file(Path(args.path), _parse_managed_pairs(args.managed))
        return 0

    if args.command == "write-deploy-state":
        write_deploy_state_file(
            Path(args.path),
            repo_url=args.repo_url,
            resolved_ref=args.resolved_ref,
            repo_archive_url=args.repo_archive_url,
            framework_archive_url=args.framework_archive_url,
            install_layout_version=args.install_layout_version,
            update_supported=args.update_supported,
        )
        return 0

    if args.command == "read-framework-dependency":
        print(json.dumps(read_framework_dependency(Path(args.path)), ensure_ascii=False))
        return 0

    if args.command == "check-update-support":
        return 0 if deploy_state_supports_update(Path(args.path)) else 1

    if args.command == "sync-install-tree":
        sync_install_tree(Path(args.source), Path(args.target), args.preserve)
        return 0

    raise ValueError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
__OPENCLAW_DEPLOY_UTILS__
