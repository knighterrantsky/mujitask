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

cd "${ROOT_DIR}"

if [[ -x "${ROOT_DIR}/.venv/bin/alembic" ]]; then
  exec "${ROOT_DIR}/.venv/bin/alembic" upgrade head
fi

exec python3 -m alembic upgrade head
