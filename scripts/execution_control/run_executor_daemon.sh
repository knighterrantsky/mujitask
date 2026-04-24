#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ROOT_DIR}/scripts/execution_control/executor.local.env"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

if [[ -x "${ROOT_DIR}/.venv/bin/automation-business-scaffold-executor" ]]; then
  EXECUTOR_CMD=("${ROOT_DIR}/.venv/bin/automation-business-scaffold-executor")
elif command -v automation-business-scaffold-executor >/dev/null 2>&1; then
  EXECUTOR_CMD=("$(command -v automation-business-scaffold-executor)")
else
  export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
  EXECUTOR_CMD=(python3 -m automation_business_scaffold.apps.daemons.executor.main)
fi

cd "${ROOT_DIR}"
exec "${EXECUTOR_CMD[@]}" "$@"
