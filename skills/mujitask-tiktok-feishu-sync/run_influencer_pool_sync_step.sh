#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export PYTHONUNBUFFERED=1

exec python3 -u "$SCRIPT_DIR/run_skill_step.py" influencer-pool-sync-submit "$@"
