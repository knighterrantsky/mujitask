#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 <install_dir> [git_ref]"
  exit 1
fi

INSTALL_DIR="$1"
GIT_REF="${2:-}"

if [[ ! -d "$INSTALL_DIR/.git" ]]; then
  echo "Not a git repository: $INSTALL_DIR"
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required but not installed."
  exit 1
fi

cd "$INSTALL_DIR"

git fetch --tags --prune

if [[ -n "$GIT_REF" ]]; then
  git checkout "$GIT_REF"
else
  git pull --ff-only
fi

uv sync

echo
echo "Update complete."
echo "Verify with:"
echo "  cd $INSTALL_DIR && uv run automation-business-scaffold-run list-tasks"
