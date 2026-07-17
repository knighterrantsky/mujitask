#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ROOT_DIR}/scripts/execution_control/executor.local.env"
runtime_migration_db_url="${BUSINESS_EXECUTION_CONTROL_RUNTIME_MIGRATION_DB_URL:-}"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

if [[ -n "${runtime_migration_db_url}" ]]; then
  BUSINESS_EXECUTION_CONTROL_DB_URL="${runtime_migration_db_url}"
  export BUSINESS_EXECUTION_CONTROL_DB_URL
  unset BUSINESS_EXECUTION_CONTROL_RUNTIME_MIGRATION_DB_URL
fi

cd "${ROOT_DIR}"

if [[ -x "${ROOT_DIR}/.venv/bin/alembic" ]]; then
  exec "${ROOT_DIR}/.venv/bin/alembic" upgrade head
fi

exec python3 -m alembic upgrade head
