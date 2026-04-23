#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ROOT_DIR}/scripts/execution_control/executor.local.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}. Copy executor.local.env.example and set BUSINESS_EXECUTION_CONTROL_DB_URL." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

DB_URL="${TEST_DATABASE_URL:-${BUSINESS_EXECUTION_CONTROL_DB_URL:-${EXECUTION_CONTROL_DB_URL:-}}}"
if [[ -z "${DB_URL}" ]]; then
  echo "Missing TEST_DATABASE_URL / BUSINESS_EXECUTION_CONTROL_DB_URL / EXECUTION_CONTROL_DB_URL." >&2
  exit 1
fi

cd "${ROOT_DIR}"
exec uv run --extra dev pytest "$@"
