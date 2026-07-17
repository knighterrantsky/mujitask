#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MIGRATION_ENV_FILE="${BUSINESS_EXECUTION_CONTROL_MIGRATION_ENV_FILE:-}"

if [[ -z "${MIGRATION_ENV_FILE}" ]]; then
  echo "BUSINESS_EXECUTION_CONTROL_MIGRATION_ENV_FILE is required." >&2
  exit 2
fi
if [[ ! -f "${MIGRATION_ENV_FILE}" || -L "${MIGRATION_ENV_FILE}" ]]; then
  echo "Migration environment file must be a regular, non-symlink file: ${MIGRATION_ENV_FILE}" >&2
  exit 2
fi
if [[ "$(uname -s)" == "Darwin" ]]; then
  MIGRATION_ENV_MODE="$(stat -f '%Lp' "${MIGRATION_ENV_FILE}")"
  MIGRATION_ENV_OWNER="$(stat -f '%u' "${MIGRATION_ENV_FILE}")"
else
  MIGRATION_ENV_MODE="$(stat -c '%a' "${MIGRATION_ENV_FILE}")"
  MIGRATION_ENV_OWNER="$(stat -c '%u' "${MIGRATION_ENV_FILE}")"
fi
if [[ ! "${MIGRATION_ENV_MODE}" =~ ^(400|600)$ ]]; then
  echo "Migration environment file must have mode 400 or 600: ${MIGRATION_ENV_FILE}" >&2
  exit 2
fi
if [[ "${MIGRATION_ENV_OWNER}" != "$(id -u)" ]]; then
  echo "Migration environment file must be owned by the current user: ${MIGRATION_ENV_FILE}" >&2
  exit 2
fi

unset BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL \
  BUSINESS_EXECUTION_CONTROL_FACT_RUNTIME_ROLE
set -a
# shellcheck disable=SC1090
source "${MIGRATION_ENV_FILE}"
set +a

if [[ -z "${BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL:-}" ]]; then
  echo "BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL is required." >&2
  exit 2
fi
if [[ -z "${BUSINESS_EXECUTION_CONTROL_FACT_RUNTIME_ROLE:-}" ]]; then
  echo "BUSINESS_EXECUTION_CONTROL_FACT_RUNTIME_ROLE is required." >&2
  exit 2
fi

cd "${ROOT_DIR}"

if [[ -x "${ROOT_DIR}/.venv/bin/alembic" ]]; then
  exec "${ROOT_DIR}/.venv/bin/alembic" -c alembic_fact.ini upgrade head
fi

exec python3 -m alembic -c alembic_fact.ini upgrade head
