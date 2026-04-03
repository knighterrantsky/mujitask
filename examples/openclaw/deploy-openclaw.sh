#!/usr/bin/env bash

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "[deploy-openclaw] ERROR: This script currently supports macOS only." >&2
  exit 1
fi

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

UV_BIN=""
PYTHON_BIN=""

log() {
  printf '[deploy-openclaw] %s\n' "$*"
}

warn() {
  printf '[deploy-openclaw] WARN: %s\n' "$*" >&2
}

fail() {
  printf '[deploy-openclaw] ERROR: %s\n' "$*" >&2
  exit 1
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
  PYTHON_BIN="$("$UV_BIN" python find 3.11 | tr -d '\r' | head -n 1)"
  [[ -n "$PYTHON_BIN" ]] || fail "Could not resolve Python 3.11 after uv installation."
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

  if curl -fsSL -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$slug/releases/latest" -o "$latest_json"; then
    local tag_name
    tag_name="$(python_json_get "tag_name" "$latest_json" | tr -d '\r')"
    if [[ -n "$tag_name" && "$tag_name" != "null" ]]; then
      printf '%s' "$tag_name"
      return 0
    fi
  fi

  curl -fsSL -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$slug/tags?per_page=1" -o "$tags_json" \
    || fail "Could not resolve latest tag for $slug."
  local tag_name
  tag_name="$(python_json_get "0.name" "$tags_json" | tr -d '\r')"
  [[ -n "$tag_name" && "$tag_name" != "null" ]] || fail "The repository $slug does not expose a latest release or tag."
  printf '%s' "$tag_name"
}

download_file() {
  local url="$1"
  local target="$2"
  curl -fsSL "$url" -o "$target"
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

prepare_target_dir() {
  local target_dir="$1"
  if [[ -d "$target_dir" ]]; then
    local backup_dir="${target_dir}.backup-$(date +%Y%m%d%H%M%S)"
    log "Existing directory detected, moving it to $backup_dir"
    mv "$target_dir" "$backup_dir"
  fi
  mkdir -p "$(dirname "$target_dir")"
  mkdir -p "$target_dir"
}

read_manifest_value() {
  local manifest_path="$1"
  local key="$2"
  "$PYTHON_BIN" - "$manifest_path" "$key" <<'PY'
from pathlib import Path
import re
import sys

manifest_path = Path(sys.argv[1])
key = sys.argv[2]
text = manifest_path.read_text(encoding="utf-8")
pattern = rf"^{re.escape(key)}:\s*\"?([^\"]+)\"?\s*$"
match = re.search(pattern, text, re.MULTILINE)
if match is None:
    raise SystemExit(f"Missing {key} in {manifest_path}")
print(match.group(1))
PY
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

write_browser_profiles() {
  local install_dir="$1"
  mkdir -p "$install_dir/config"
  cat > "$install_dir/config/browser_profiles.json" <<'JSON'
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

write_skill_local_env() {
  local skill_dir="$1"
  local install_dir="$2"
  local table_url="$3"
  local token="$4"

  cat > "$skill_dir/skill.local.env" <<EOF
INSTALL_DIR=$install_dir
TABLE_URL=$table_url
FEISHU_ACCESS_TOKEN=$token
EOF
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

required = {"tiktok_product_link_cleanup", "tiktok_feishu_batch_sync"}
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
    "run_cleanup.sh"
    "run_cleanup.ps1"
    "run_batch_sync.sh"
    "run_batch_sync.ps1"
    "start_browser_cdp.sh"
    "start_browser_cdp.ps1"
  )

  local file_name
  for file_name in "${required_files[@]}"; do
    [[ -f "$target_skill_dir/$file_name" ]] || fail "Smoke check failed: $target_skill_dir/$file_name is missing."
  done
}

main() {
  ensure_uv
  ensure_python_311

  local repo_url tag install_dir table_url token archive_url github_slug resolved_ref
  repo_url="$(prompt "Repo URL")"
  tag="$(prompt "Tag (leave blank to auto-resolve latest)" "")"
  install_dir="$(prompt "Install directory" "$HOME/apps/mujitask")"
  table_url="$(prompt "Feishu table URL")"
  token="$(prompt_secret "Feishu access token")"

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

  prepare_target_dir "$install_dir"
  cp -R "$project_root"/. "$install_dir"/

  local manifest_path="$install_dir/.platform/platform-manifest.yaml"
  [[ -f "$manifest_path" ]] || fail "Missing $manifest_path after extraction."

  local framework_repo_url framework_ref framework_archive_url framework_archive framework_root framework_slug
  framework_repo_url="$(read_manifest_value "$manifest_path" "framework_repo_url" | tr -d '\r')"
  framework_ref="$(read_manifest_value "$manifest_path" "framework_commit" | tr -d '\r')"
  framework_archive="$TMP_ROOT/framework-archive.zip"

  if framework_slug="$(parse_github_slug "$framework_repo_url" 2>/dev/null)"; then
    framework_archive_url="https://api.github.com/repos/$framework_slug/zipball/$framework_ref"
  else
    framework_archive_url="$(prompt "Framework archive URL for automation-framework")"
    local lowered_framework
    lowered_framework="$(to_lower "$framework_archive_url")"
    if [[ "$lowered_framework" == *.tar.gz || "$lowered_framework" == *.tgz || "$lowered_framework" == *.tar ]]; then
      framework_archive="$TMP_ROOT/framework-archive.tar.gz"
    fi
  fi

  log "Downloading pinned automation-framework source"
  download_file "$framework_archive_url" "$framework_archive"
  framework_root="$(extract_archive "$framework_archive" "$TMP_ROOT/framework-extracted")"

  log "Creating project virtual environment"
  "$UV_BIN" venv --python 3.11 "$install_dir/.venv"

  local venv_python="$install_dir/.venv/bin/python"
  [[ -x "$venv_python" ]] || fail "Virtual environment python was not created."

  log "Installing pinned automation-framework from local source"
  "$UV_BIN" pip install --python "$venv_python" "$framework_root"

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

  mkdir -p "$install_dir/runtime/cli_runs" "$install_dir/runtime/artifacts" "$install_dir/runtime/downloads"
  write_browser_profiles "$install_dir"

  local chrome_bin
  chrome_bin="$(detect_chrome_bin || true)"
  if [[ -z "$chrome_bin" ]]; then
    warn "Google Chrome was not found."
    warn "Install Chrome and rerun this deployment script."
    exit 2
  fi

  local openclaw_skills_dir="$HOME/.openclaw/workspace/skills"
  local source_skill_dir="$install_dir/skills/mujitask-tiktok-feishu-sync"
  local target_skill_dir="$openclaw_skills_dir/mujitask-tiktok-feishu-sync"

  [[ -d "$source_skill_dir" ]] || fail "Missing skill bundle at $source_skill_dir."

  prepare_target_dir "$target_skill_dir"
  cp -R "$source_skill_dir"/. "$target_skill_dir"/
  write_skill_local_env "$target_skill_dir" "$install_dir" "$table_url" "$token"

  smoke_check "$install_dir" "$target_skill_dir"

  log "Deployment completed."
  log "Installed ref: $resolved_ref"
  log "Project directory: $install_dir"
  log "OpenClaw skill directory: $target_skill_dir"
  log "Chrome binary: $chrome_bin"
}

main "$@"
