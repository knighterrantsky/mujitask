#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
TEMPLATE_DIR="${ROOT_DIR}/config/deployment/launchd"
LOG_DIR="${ROOT_DIR}/runtime/phase1_daemons"
ENV_FILE="${ROOT_DIR}/scripts/execution_control/executor.local.env"
UID_VALUE="$(id -u)"

LABELS=(
  "com.happyzhao.mujitask.phase1-executor"
  "com.happyzhao.mujitask.browser-runloop"
  "com.happyzhao.mujitask.outbox-dispatcher"
)

mkdir -p "${LAUNCH_AGENTS_DIR}" "${LOG_DIR}"

chmod +x "${ROOT_DIR}/scripts/execution_control/run_launchd_agent.sh"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

"${ROOT_DIR}/.venv/bin/python" - <<'PY'
import os
from automation_business_scaffold.flows.phase1_runtime_store import Phase1RuntimeStore

db_url = os.environ.get("BUSINESS_EXECUTION_CONTROL_DB_URL", "")
db_path = os.environ.get("BUSINESS_EXECUTION_CONTROL_DB_PATH", "")
Phase1RuntimeStore(db_url=db_url, db_path=db_path)
print("schema_ready")
PY

python3 - <<'PY' "${ROOT_DIR}" "${TEMPLATE_DIR}" "${LAUNCH_AGENTS_DIR}"
import sys
from pathlib import Path

root_dir = Path(sys.argv[1])
template_dir = Path(sys.argv[2])
launch_agents_dir = Path(sys.argv[3])

for template_path in sorted(template_dir.glob("*.plist.template")):
    rendered = template_path.read_text(encoding="utf-8").replace("__ROOT_DIR__", str(root_dir))
    dest_path = launch_agents_dir / template_path.name.replace(".template", "")
    dest_path.write_text(rendered, encoding="utf-8")
    print(dest_path)
PY

pkill -f 'automation_business_scaffold.phase1_executor_daemon' >/dev/null 2>&1 || true
pkill -f 'automation_business_scaffold.browser_runloop' >/dev/null 2>&1 || true
pkill -f 'automation_business_scaffold.outbox_dispatcher' >/dev/null 2>&1 || true

sleep 1

for label in "${LABELS[@]}"; do
  plist_path="${LAUNCH_AGENTS_DIR}/${label}.plist"
  launchctl bootout "gui/${UID_VALUE}" "${plist_path}" >/dev/null 2>&1 || true
done

for label in "${LABELS[@]}"; do
  plist_path="${LAUNCH_AGENTS_DIR}/${label}.plist"
  launchctl bootstrap "gui/${UID_VALUE}" "${plist_path}"
  launchctl kickstart -k "gui/${UID_VALUE}/${label}"
done

launchctl list | grep 'com.happyzhao.mujitask' || true
