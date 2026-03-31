#!/usr/bin/env bash

set -euo pipefail

usage() {
  echo "Usage: $0 <repo_url> <install_dir> [git_ref]"
}

require_command() {
  local command_name="$1"

  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "$command_name is required but not installed."
    exit 1
  fi
}

read_manifest_value() {
  local key="$1"

  python3 - "$key" <<'PY'
from pathlib import Path
import re
import sys

key = sys.argv[1]
text = Path(".platform/platform-manifest.yaml").read_text(encoding="utf-8")
pattern = rf"^{re.escape(key)}:\s*\"?([^\"]+)\"?\s*$"
match = re.search(pattern, text, re.MULTILINE)
if match is None:
    raise SystemExit(f"Missing {key} in .platform/platform-manifest.yaml")
print(match.group(1))
PY
}

default_branch_name() {
  git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's@^origin/@@'
}

build_framework_requirement() {
  local framework_repo_url="$1"
  local framework_ref="$2"

  if [[ "$framework_repo_url" == git+* ]]; then
    printf 'automation-framework @ %s@%s' "$framework_repo_url" "$framework_ref"
    return
  fi

  printf 'automation-framework @ git+%s@%s' "$framework_repo_url" "$framework_ref"
}

install_with_framework_override() {
  local framework_repo_url="$1"
  local framework_ref="$2"
  local python_bin=".venv/bin/python"
  local framework_requirement

  framework_requirement="$(build_framework_requirement "$framework_repo_url" "$framework_ref")"

  uv venv --python 3.11 .venv
  uv pip install --python "$python_bin" --reinstall "$framework_requirement"
  uv pip install --python "$python_bin" --reinstall -e . --extra dev --no-deps
}

install_playwright_if_available() {
  local python_bin=".venv/bin/python"

  if [[ ! -x "$python_bin" ]]; then
    return
  fi

  if "$python_bin" -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('playwright') else 1)"; then
    "$python_bin" -m playwright install chromium
  fi
}

if [[ $# -lt 2 || $# -gt 3 ]]; then
  usage
  exit 1
fi

REPO_URL="$1"
INSTALL_DIR="$2"
GIT_REF="${3:-}"

require_command git
require_command uv
require_command python3

mkdir -p "$(dirname "$INSTALL_DIR")"

if [[ -d "$INSTALL_DIR/.git" ]]; then
  echo "Repository already exists at $INSTALL_DIR; updating in place."
  CURRENT_REMOTE_URL="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
  if [[ -n "$CURRENT_REMOTE_URL" && "$CURRENT_REMOTE_URL" != "$REPO_URL" ]]; then
    git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL"
  fi
else
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

git fetch --tags --prune

if [[ -n "$GIT_REF" ]]; then
  git checkout "$GIT_REF"
else
  DEFAULT_BRANCH="$(default_branch_name)"
  if [[ -n "$DEFAULT_BRANCH" ]]; then
    if ! git symbolic-ref -q HEAD >/dev/null 2>&1; then
      git checkout "$DEFAULT_BRANCH"
    fi
    git pull --ff-only origin "$DEFAULT_BRANCH"
  fi
fi

FRAMEWORK_REPO_URL="${FRAMEWORK_REPO_URL:-}"
FRAMEWORK_GIT_REF="${FRAMEWORK_GIT_REF:-$(read_manifest_value framework_commit)}"

if [[ -n "$FRAMEWORK_REPO_URL" ]]; then
  install_with_framework_override "$FRAMEWORK_REPO_URL" "$FRAMEWORK_GIT_REF"
else
  uv sync
fi

install_playwright_if_available

mkdir -p runtime/cli_runs runtime/artifacts runtime/downloads

echo
echo "Install complete."
echo "Next steps:"
echo "1. Export FEISHU_ACCESS_TOKEN on the Mac mini."
echo "2. Prepare OpenClaw params (table_url / product_url / product_urls) and optional local config notes."
echo "3. Verify with:"
echo "   cd $INSTALL_DIR && .venv/bin/automation-business-scaffold-run list-tasks"
