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
