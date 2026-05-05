#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <python-module>" >&2
  exit 1
fi

MODULE_NAME="$1"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ROOT_DIR}/scripts/execution_control/executor.local.env"

cd "${ROOT_DIR}"
mkdir -p "${ROOT_DIR}/runtime/daemons"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
  exec "${ROOT_DIR}/.venv/bin/python" -m "${MODULE_NAME}" "${@:2}"
fi

exec python3 -m "${MODULE_NAME}" "${@:2}"
