#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "Usage: $0 <repo_url> <install_dir> [git_ref]"
  exit 1
fi

REPO_URL="$1"
INSTALL_DIR="$2"
GIT_REF="${3:-}"

if ! command -v git >/dev/null 2>&1; then
  echo "git is required but not installed."
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required but not installed."
  exit 1
fi

mkdir -p "$(dirname "$INSTALL_DIR")"

if [[ -d "$INSTALL_DIR/.git" ]]; then
  echo "Repository already exists at $INSTALL_DIR"
else
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

if [[ -n "$GIT_REF" ]]; then
  git fetch --tags --prune
  git checkout "$GIT_REF"
fi

uv sync

mkdir -p runtime/cli_runs runtime/artifacts runtime/downloads

echo
echo "Install complete."
echo "Next steps:"
echo "1. Export FEISHU_ACCESS_TOKEN on the Mac mini."
echo "2. Prepare a local customer config file."
echo "3. Verify with:"
echo "   cd $INSTALL_DIR && uv run automation-business-scaffold-run list-tasks"
