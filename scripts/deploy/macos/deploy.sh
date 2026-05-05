#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ENV_FILE="${MUJITASK_DEPLOY_ENV_FILE:-${SOURCE_DIR}/scripts/deploy/macos/deploy.local.env}"
PROJECT_DIR=""

OPENCLAW_LOG_PREFIX="mujitask-macos-deploy"
# shellcheck source=../../../examples/openclaw/openclaw_deploy_common.sh
source "${SOURCE_DIR}/examples/openclaw/openclaw_deploy_common.sh"

log() {
  printf '[mujitask-macos-deploy] %s\n' "$*"
}

fail_deploy() {
  printf '[mujitask-macos-deploy] ERROR: %s\n' "$*" >&2
  exit 1
}

load_deploy_env() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    fail_deploy "Missing ${ENV_FILE}. Copy scripts/deploy/macos/deploy.local.env.example to scripts/deploy/macos/deploy.local.env and fill it in."
  fi

  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
}

config_value() {
  local primary="$1"
  local fallback="${2:-}"
  local default_value="${3:-}"
  local value="${!primary:-}"
  if [[ -z "${value}" && -n "${fallback}" ]]; then
    value="${!fallback:-}"
  fi
  if [[ -z "${value}" ]]; then
    value="${default_value}"
  fi
  printf '%s' "${value}"
}

sql_literal() {
  printf "%s" "$1" | sed "s/'/''/g"
}

sql_identifier() {
  printf "%s" "$1" | sed 's/"/""/g'
}

urlencode_component() {
  "$PYTHON_BIN" -c 'from urllib.parse import quote; import sys; print(quote(sys.argv[1], safe=""))' "$1"
}

compose_feishu_table_url() {
  local base_url="$1"
  local table_id="$2"
  local view_id="${3:-}"
  "$PYTHON_BIN" - "${base_url}" "${table_id}" "${view_id}" <<'PY'
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import sys

base_url, table_id, view_id = [item.strip() for item in sys.argv[1:4]]
if not base_url or not table_id:
    print("")
    raise SystemExit(0)
parsed = urlparse(base_url)
query = dict(parse_qsl(parsed.query, keep_blank_values=True))
query["table"] = table_id
if view_id:
    query["view"] = view_id
elif "view" in query:
    query.pop("view", None)
path = parsed.path.rstrip("/") or parsed.path
print(urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, urlencode(query), parsed.fragment)))
PY
}

resolve_feishu_business_table_url() {
  local env_slug="$1"
  local base_url="$2"
  local table_id view_id

  [[ -n "${base_url}" ]] || fail_deploy "Missing MUJITASK_FEISHU_BASE_URL in ${ENV_FILE}."
  table_id="$(require_config_value "MUJITASK_FEISHU_${env_slug}_TABLE_ID")"
  view_id="$(require_config_value "MUJITASK_FEISHU_${env_slug}_VIEW_ID")"
  compose_feishu_table_url "${base_url}" "${table_id}" "${view_id}"
}

resolve_path() {
  local raw_path="$1"
  local base_dir="${2:-${SOURCE_DIR}}"
  if [[ "${raw_path}" == /* ]]; then
    printf '%s' "${raw_path}"
  else
    printf '%s/%s' "${base_dir}" "${raw_path}"
  fi
}

require_config_value() {
  local primary="$1"
  local fallback="${2:-}"
  local value
  value="$(config_value "${primary}" "${fallback}" "")"
  [[ -n "${value}" ]] || fail_deploy "Missing ${primary}${fallback:+ / ${fallback}} in ${ENV_FILE}."
  printf '%s' "${value}"
}

prepare_project_tree() {
  local install_dir="$1"
  local source_real target_real=""

  mkdir -p "${install_dir}"
  source_real="$(cd "${SOURCE_DIR}" && pwd -P)"
  target_real="$(cd "${install_dir}" && pwd -P)"
  if [[ "${source_real}" == "${target_real}" ]]; then
    log "Using source checkout as project install directory: ${install_dir}"
    return 0
  fi

  if directory_has_entries "${install_dir}" \
    && [[ ! -f "${install_dir}/runtime/deployment/openclaw-deploy.env" ]] \
    && [[ "${MUJITASK_ALLOW_NONEMPTY_INSTALL_DIR:-0}" != "1" ]]; then
    fail_deploy "Install directory is not empty and is not marked as a managed mujitask install: ${install_dir}. Choose an empty directory or set MUJITASK_ALLOW_NONEMPTY_INSTALL_DIR=1 after confirming it is safe."
  fi

  log "Syncing project source into install directory: ${install_dir}"
  "${PYTHON_BIN}" "${OPENCLAW_DEPLOY_UTILS}" sync-install-tree \
    --source "${SOURCE_DIR}" \
    --target "${install_dir}" \
    --preserve ".venv" \
    --preserve "runtime" \
    --preserve ".env" \
    --preserve "config/browser_profiles.json" \
    --preserve "scripts/execution_control/executor.local.env" \
    --preserve "scripts/deploy/macos/deploy.local.env"
}

ensure_project_install() {
  local install_dir="$1"
  ensure_uv
  ensure_python_311

  log "Creating project virtual environment"
  "${UV_BIN}" venv --python 3.11 "${install_dir}/.venv" >/dev/null

  local venv_python="${install_dir}/.venv/bin/python"
  [[ -x "${venv_python}" ]] || fail_deploy "Virtual environment python was not created: ${venv_python}"

  install_framework_from_pyproject "${install_dir}/pyproject.toml" "${venv_python}"

  local project_deps=()
  local dep
  while IFS= read -r dep; do
    [[ -n "${dep}" ]] && project_deps+=("${dep}")
  done < <(read_project_dependencies "${install_dir}/pyproject.toml")

  if ((${#project_deps[@]} > 0)); then
    log "Installing project runtime dependencies"
    "${UV_BIN}" pip install --python "${venv_python}" "${project_deps[@]}"
  fi

  log "Installing project package"
  "${UV_BIN}" pip install --python "${venv_python}" -e "${install_dir}" --no-deps

  install_project_node_dependencies "${install_dir}"

  log "Installing Playwright Chromium"
  "${venv_python}" -m playwright install chromium
}

prepare_local_files() {
  local install_dir="$1"
  mkdir -p \
    "${install_dir}/runtime/cli_runs" \
    "${install_dir}/runtime/artifacts" \
    "${install_dir}/runtime/downloads" \
    "${install_dir}/runtime/phase1_daemons" \
    "${install_dir}/runtime/execution_control"

  if [[ ! -f "${install_dir}/.env" && -f "${install_dir}/.env.example" ]]; then
    cp "${install_dir}/.env.example" "${install_dir}/.env"
  fi
  if [[ ! -f "${install_dir}/config/browser_profiles.json" && -f "${install_dir}/config/browser_profiles.example.json" ]]; then
    cp "${install_dir}/config/browser_profiles.example.json" "${install_dir}/config/browser_profiles.json"
  fi
}

ensure_brew_formula() {
  local formula="$1"
  if brew list --versions "${formula}" >/dev/null 2>&1; then
    return 0
  fi
  log "Installing Homebrew formula: ${formula}"
  brew install "${formula}"
}

homebrew_formula_bin() {
  local formula="$1"
  local bin_name="$2"
  local prefix=""
  prefix="$(brew --prefix "${formula}" 2>/dev/null || true)"
  if [[ -n "${prefix}" && -x "${prefix}/bin/${bin_name}" ]]; then
    printf '%s' "${prefix}/bin/${bin_name}"
    return 0
  fi
  if command -v "${bin_name}" >/dev/null 2>&1; then
    command -v "${bin_name}"
    return 0
  fi
  return 1
}

validate_pg_database_name() {
  local db_name="$1"
  if [[ ! "${db_name}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    fail_deploy "Invalid MUJITASK_POSTGRES_DB=${db_name}. Use letters, numbers, and underscores, starting with a letter or underscore."
  fi
}

ensure_native_postgres() {
  command -v brew >/dev/null 2>&1 || fail_deploy "Homebrew is required for native macOS Postgres installation."

  local formula="${MUJITASK_POSTGRES_FORMULA:-postgresql@17}"
  local db_name="${MUJITASK_POSTGRES_DB:-automation_business_scaffold}"
  local db_user="${MUJITASK_POSTGRES_USER:-mujitask}"
  local db_password="${MUJITASK_POSTGRES_PASSWORD:-mujitask}"
  local admin_user="${MUJITASK_POSTGRES_ADMIN_USER:-$(id -un)}"
  local port="${MUJITASK_POSTGRES_PORT:-5432}"
  local socket_dir="${MUJITASK_POSTGRES_SOCKET_DIR:-/tmp}"
  validate_pg_database_name "${db_name}"

  ensure_brew_formula "${formula}"

  log "Starting Postgres via Homebrew services: ${formula}"
  brew services start "${formula}" >/dev/null

  local psql_bin createdb_bin
  psql_bin="$(homebrew_formula_bin "${formula}" psql)" || fail_deploy "Could not resolve psql from ${formula}."
  createdb_bin="$(homebrew_formula_bin "${formula}" createdb)" || fail_deploy "Could not resolve createdb from ${formula}."

  local last_error=""
  for _ in {1..90}; do
    if last_error="$(PGHOST="${socket_dir}" PGPORT="${port}" PGUSER="${admin_user}" "${psql_bin}" -d postgres -Atqc "select 1" 2>&1 >/dev/null)"; then
      last_error=""
      break
    fi
    sleep 1
  done
  [[ -z "${last_error}" ]] || fail_deploy "Postgres did not become ready: ${last_error}"

  local db_user_sql db_password_sql db_name_sql db_user_ident db_name_ident
  db_user_sql="$(sql_literal "${db_user}")"
  db_password_sql="$(sql_literal "${db_password}")"
  db_name_sql="$(sql_literal "${db_name}")"
  db_user_ident="$(sql_identifier "${db_user}")"
  db_name_ident="$(sql_identifier "${db_name}")"

  if PGHOST="${socket_dir}" PGPORT="${port}" PGUSER="${admin_user}" "${psql_bin}" \
    -d postgres \
    -Atqc "SELECT 1 FROM pg_roles WHERE rolname = '${db_user_sql}'" | grep -q '^1$'; then
    log "Postgres role already exists: ${db_user}"
    PGHOST="${socket_dir}" PGPORT="${port}" PGUSER="${admin_user}" "${psql_bin}" \
      -d postgres \
      -v ON_ERROR_STOP=1 \
      -c "ALTER ROLE \"${db_user_ident}\" WITH LOGIN PASSWORD '${db_password_sql}'" >/dev/null
  else
    log "Creating Postgres role: ${db_user}"
    PGHOST="${socket_dir}" PGPORT="${port}" PGUSER="${admin_user}" "${psql_bin}" \
      -d postgres \
      -v ON_ERROR_STOP=1 \
      -c "CREATE ROLE \"${db_user_ident}\" WITH LOGIN PASSWORD '${db_password_sql}'" >/dev/null
  fi

  if PGHOST="${socket_dir}" PGPORT="${port}" PGUSER="${admin_user}" "${psql_bin}" \
    -d postgres \
    -Atqc "SELECT 1 FROM pg_database WHERE datname = '${db_name_sql}'" | grep -q '^1$'; then
    log "Postgres database already exists: ${db_name}"
  else
    log "Creating Postgres database: ${db_name} owned by ${db_user}"
    PGHOST="${socket_dir}" PGPORT="${port}" PGUSER="${admin_user}" "${createdb_bin}" -O "${db_user}" "${db_name}"
  fi

  PGHOST="${socket_dir}" PGPORT="${port}" PGUSER="${admin_user}" "${psql_bin}" \
    -d "${db_name}" \
    -v ON_ERROR_STOP=1 \
    -c "GRANT ALL PRIVILEGES ON DATABASE \"${db_name_ident}\" TO \"${db_user_ident}\"" \
    -c "GRANT ALL ON SCHEMA public TO \"${db_user_ident}\"" >/dev/null
}

ensure_native_minio() {
  command -v brew >/dev/null 2>&1 || fail_deploy "Homebrew is required for native macOS MinIO installation."

  ensure_brew_formula "minio"

  local minio_bin
  minio_bin="$(homebrew_formula_bin minio minio)" || fail_deploy "Could not resolve minio binary."

  local bind="${MUJITASK_MINIO_BIND:-127.0.0.1}"
  local port="${MUJITASK_MINIO_PORT:-9000}"
  local console_bind="${MUJITASK_MINIO_CONSOLE_BIND:-127.0.0.1}"
  local console_port="${MUJITASK_MINIO_CONSOLE_PORT:-9001}"
  local root_user="${MUJITASK_MINIO_ROOT_USER:-minioadmin}"
  local root_password="${MUJITASK_MINIO_ROOT_PASSWORD:-minioadmin}"
  local data_dir="${MUJITASK_MINIO_DATA_DIR:-${PROJECT_DIR}/runtime/minio/data}"

  if [[ "${data_dir}" != /* ]]; then
    data_dir="${PROJECT_DIR}/${data_dir}"
  fi

  local label="com.happyzhao.mujitask.minio"
  local launch_agents_dir="${HOME}/Library/LaunchAgents"
  local plist_path="${launch_agents_dir}/${label}.plist"
  local log_dir="${PROJECT_DIR}/runtime/native_services"
  local uid_value
  uid_value="$(id -u)"

  if ! launchctl print "gui/${uid_value}/${label}" >/dev/null 2>&1; then
    if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
      log "MinIO API port ${port} is already listening; using the existing local MinIO service."
      return 0
    fi
  fi

  mkdir -p "${launch_agents_dir}" "${log_dir}" "${data_dir}"

  log "Installing MinIO launchd service"
  "${PYTHON_BIN}" - \
    "${plist_path}" \
    "${label}" \
    "${minio_bin}" \
    "${data_dir}" \
    "${bind}:${port}" \
    "${console_bind}:${console_port}" \
    "${root_user}" \
    "${root_password}" \
    "${PROJECT_DIR}" \
    "${log_dir}/minio.stdout.log" \
    "${log_dir}/minio.stderr.log" <<'PY'
from pathlib import Path
import plistlib
import sys

(
    plist_path,
    label,
    minio_bin,
    data_dir,
    address,
    console_address,
    root_user,
    root_password,
    working_dir,
    stdout_path,
    stderr_path,
) = sys.argv[1:]

payload = {
    "Label": label,
    "ProgramArguments": [
        minio_bin,
        "server",
        data_dir,
        "--address",
        address,
        "--console-address",
        console_address,
    ],
    "EnvironmentVariables": {
        "MINIO_ROOT_USER": root_user,
        "MINIO_ROOT_PASSWORD": root_password,
    },
    "WorkingDirectory": working_dir,
    "RunAtLoad": True,
    "KeepAlive": True,
    "ProcessType": "Background",
    "StandardOutPath": stdout_path,
    "StandardErrorPath": stderr_path,
}

Path(plist_path).write_bytes(plistlib.dumps(payload, sort_keys=False))
PY

  launchctl bootout "gui/${uid_value}" "${plist_path}" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/${uid_value}" "${plist_path}"
  launchctl kickstart -k "gui/${uid_value}/${label}"
}

start_runtime_services() {
  local artifact_store_provider="$1"

  if [[ "${MUJITASK_RUNTIME_MODE:-native}" != "native" ]]; then
    log "Skipping native runtime services because MUJITASK_RUNTIME_MODE=${MUJITASK_RUNTIME_MODE:-external}"
    return 0
  fi

  ensure_native_postgres
  if [[ "${artifact_store_provider}" == "minio" ]]; then
    ensure_native_minio
  else
    log "Skipping MinIO service because artifact store provider is ${artifact_store_provider}"
  fi
}

wait_for_runtime() {
  local venv_python="${PROJECT_DIR}/.venv/bin/python"
  local db_url="$1"
  local provider="$2"
  local endpoint="$3"
  local access_key="$4"
  local secret_key="$5"
  local secure="$6"
  local bucket="$7"
  local create_bucket="$8"

  log "Waiting for runtime dependencies"
  "${venv_python}" - \
    "${db_url}" \
    "${provider}" \
    "${endpoint}" \
    "${access_key}" \
    "${secret_key}" \
    "${secure}" \
    "${bucket}" \
    "${create_bucket}" <<'PY'
import sys
import time

from sqlalchemy import create_engine, text

db_url, provider, endpoint, access_key, secret_key, secure, bucket, create_bucket = sys.argv[1:]
last_error = None

for _ in range(90):
    try:
        engine = create_engine(db_url, future=True, pool_pre_ping=True)
        with engine.connect() as connection:
            connection.execute(text("select 1"))
        last_error = None
        break
    except Exception as exc:
        last_error = exc
        time.sleep(1)
else:
    raise SystemExit(f"Database is not ready: {last_error}")

if provider.strip().lower() == "minio":
    from minio import Minio

    client = Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure.strip().lower() in {"1", "true", "yes", "on"},
    )
    last_error = None
    for _ in range(90):
        try:
            exists = client.bucket_exists(bucket)
            if not exists and create_bucket.strip().lower() in {"1", "true", "yes", "on"}:
                client.make_bucket(bucket)
            elif not exists:
                raise RuntimeError(f"Bucket does not exist: {bucket}")
            break
        except Exception as exc:
            last_error = exc
            time.sleep(1)
    else:
        raise SystemExit(f"MinIO is not ready: {last_error}")

print("runtime_ready")
PY
}

install_agent_skill() {
  local install_dir="$1"
  local skills_dir="$2"
  local token="$3"
  local browser_profile_ref="$4"
  local fastmoss_phone="$5"
  local fastmoss_password="$6"
  local db_url="$7"
  local artifact_root="$8"
  local artifact_bucket="$9"
  local requested_by="${10}"
  local notification_channel_code="${11}"
  local openclaw_agent_id="${12}"
  local openclaw_state_dir="${13}"
  local feishu_base_url="${14}"
  local tk_selection_table_id="${15}"
  local tk_selection_view_id="${16}"
  local tk_competitor_table_id="${17}"
  local tk_competitor_view_id="${18}"
  local tk_influencer_pool_table_id="${19}"
  local tk_influencer_pool_view_id="${20}"
  local tk_influencer_outreach_table_id="${21}"
  local tk_influencer_outreach_view_id="${22}"
  local tk_hot_video_table_id="${23}"
  local tk_hot_video_view_id="${24}"

  local source_skill_dir="${install_dir}/skills/mujitask-tiktok-feishu-sync"
  local target_skill_dir="${skills_dir}/mujitask-tiktok-feishu-sync"

  [[ -d "${source_skill_dir}" ]] || fail_deploy "Missing skill bundle at ${source_skill_dir}."

  mkdir -p "${skills_dir}"
  local source_real target_real
  source_real="$(cd "${source_skill_dir}" && pwd -P)"
  if [[ -d "${target_skill_dir}" ]]; then
    target_real="$(cd "${target_skill_dir}" && pwd -P)"
  else
    target_real=""
  fi

  if [[ "${source_real}" != "${target_real}" ]]; then
    local previous_skill_env="${TMP_ROOT}/previous-skill.local.env"
    if [[ -f "${target_skill_dir}/skill.local.env" ]]; then
      cp "${target_skill_dir}/skill.local.env" "${previous_skill_env}"
    fi
    replace_target_dir "${target_skill_dir}"
    cp -R "${source_skill_dir}"/. "${target_skill_dir}"/
    if [[ -f "${previous_skill_env}" ]]; then
      cp "${previous_skill_env}" "${target_skill_dir}/skill.local.env"
    fi
  fi

  seed_key_value_file_from_example "${target_skill_dir}/skill.local.env" "${target_skill_dir}/skill.local.env.example"
  merge_key_value_file \
    "${target_skill_dir}/skill.local.env" \
    "INSTALL_DIR=$(quote_env_value "${install_dir}")" \
    "MUJITASK_FEISHU_BASE_URL=$(quote_env_value "${feishu_base_url}")" \
    "MUJITASK_FEISHU_TK_SELECTION_TABLE_ID=$(quote_env_value "${tk_selection_table_id}")" \
    "MUJITASK_FEISHU_TK_SELECTION_VIEW_ID=$(quote_env_value "${tk_selection_view_id}")" \
    "MUJITASK_FEISHU_TK_COMPETITOR_TABLE_ID=$(quote_env_value "${tk_competitor_table_id}")" \
    "MUJITASK_FEISHU_TK_COMPETITOR_VIEW_ID=$(quote_env_value "${tk_competitor_view_id}")" \
    "MUJITASK_FEISHU_TK_INFLUENCER_POOL_TABLE_ID=$(quote_env_value "${tk_influencer_pool_table_id}")" \
    "MUJITASK_FEISHU_TK_INFLUENCER_POOL_VIEW_ID=$(quote_env_value "${tk_influencer_pool_view_id}")" \
    "MUJITASK_FEISHU_TK_INFLUENCER_OUTREACH_TABLE_ID=$(quote_env_value "${tk_influencer_outreach_table_id}")" \
    "MUJITASK_FEISHU_TK_INFLUENCER_OUTREACH_VIEW_ID=$(quote_env_value "${tk_influencer_outreach_view_id}")" \
    "MUJITASK_FEISHU_TK_HOT_VIDEO_TABLE_ID=$(quote_env_value "${tk_hot_video_table_id}")" \
    "MUJITASK_FEISHU_TK_HOT_VIDEO_VIEW_ID=$(quote_env_value "${tk_hot_video_view_id}")" \
    "MUJITASK_FEISHU_ACCESS_TOKEN=$(quote_env_value "${token}")" \
    "BROWSER_PROFILE_REF=$(quote_env_value "${browser_profile_ref}")" \
    "FASTMOSS_PHONE=$(quote_env_value "${fastmoss_phone}")" \
    "FASTMOSS_PASSWORD=$(quote_env_value "${fastmoss_password}")" \
    "EXECUTION_CONTROL_DB_URL=$(quote_env_value "${db_url}")" \
    "EXECUTION_CONTROL_ARTIFACT_ROOT=$(quote_env_value "${artifact_root}")" \
    "EXECUTION_CONTROL_ARTIFACT_BUCKET=$(quote_env_value "${artifact_bucket}")" \
    "EXECUTION_CONTROL_REQUESTED_BY=$(quote_env_value "${requested_by}")" \
    "NOTIFICATION_CHANNEL_CODE=$(quote_env_value "${notification_channel_code}")" \
    "OPENCLAW_AGENT_ID=$(quote_env_value "${openclaw_agent_id}")" \
    "OPENCLAW_STATE_DIR=$(quote_env_value "${openclaw_state_dir}")"
  INSTALLED_SKILL_DIR="${target_skill_dir}"
}

main() {
  load_deploy_env
  MUJITASK_PREFLIGHT_REQUIRE_ENV=1 MUJITASK_DEPLOY_ENV_FILE="${ENV_FILE}" bash "${SOURCE_DIR}/scripts/deploy/macos/preflight.sh"

  local install_dir skills_dir agent_type
  install_dir="$(resolve_path "$(config_value MUJITASK_INSTALL_DIR INSTALL_DIR "${HOME}/apps/mujitask")" "${SOURCE_DIR}")"
  skills_dir="$(resolve_path "$(require_config_value MUJITASK_SKILLS_DIR SKILLS_INSTALL_DIR)" "${HOME}")"
  agent_type="$(config_value MUJITASK_AGENT_TYPE AGENT_TYPE "generic")"
  PROJECT_DIR="${install_dir}"

  local token browser_profile_ref fastmoss_phone fastmoss_password
  token="$(require_config_value MUJITASK_FEISHU_ACCESS_TOKEN)"
  browser_profile_ref="$(config_value MUJITASK_BROWSER_PROFILE_REF BROWSER_PROFILE_REF "roxy-tiktok")"
  fastmoss_phone="$(require_config_value MUJITASK_FASTMOSS_PHONE FASTMOSS_PHONE)"
  fastmoss_password="$(require_config_value MUJITASK_FASTMOSS_PASSWORD FASTMOSS_PASSWORD)"

  ensure_uv
  ensure_python_311

  local feishu_base_url
  local tk_selection_table_id tk_selection_view_id
  local tk_competitor_table_id tk_competitor_view_id
  local tk_influencer_pool_table_id tk_influencer_pool_view_id
  local tk_influencer_outreach_table_id tk_influencer_outreach_view_id
  local tk_hot_video_table_id tk_hot_video_view_id
  feishu_base_url="$(require_config_value MUJITASK_FEISHU_BASE_URL)"
  tk_selection_table_id="$(require_config_value MUJITASK_FEISHU_TK_SELECTION_TABLE_ID)"
  tk_selection_view_id="$(require_config_value MUJITASK_FEISHU_TK_SELECTION_VIEW_ID)"
  tk_competitor_table_id="$(require_config_value MUJITASK_FEISHU_TK_COMPETITOR_TABLE_ID)"
  tk_competitor_view_id="$(require_config_value MUJITASK_FEISHU_TK_COMPETITOR_VIEW_ID)"
  tk_influencer_pool_table_id="$(require_config_value MUJITASK_FEISHU_TK_INFLUENCER_POOL_TABLE_ID)"
  tk_influencer_pool_view_id="$(require_config_value MUJITASK_FEISHU_TK_INFLUENCER_POOL_VIEW_ID)"
  tk_influencer_outreach_table_id="$(require_config_value MUJITASK_FEISHU_TK_INFLUENCER_OUTREACH_TABLE_ID)"
  tk_influencer_outreach_view_id="$(require_config_value MUJITASK_FEISHU_TK_INFLUENCER_OUTREACH_VIEW_ID)"
  tk_hot_video_table_id="$(require_config_value MUJITASK_FEISHU_TK_HOT_VIDEO_TABLE_ID)"
  tk_hot_video_view_id="$(require_config_value MUJITASK_FEISHU_TK_HOT_VIDEO_VIEW_ID)"

  local postgres_port postgres_db postgres_user postgres_password
  postgres_port="${MUJITASK_POSTGRES_PORT:-5432}"
  postgres_db="${MUJITASK_POSTGRES_DB:-automation_business_scaffold}"
  postgres_user="${MUJITASK_POSTGRES_USER:-mujitask}"
  postgres_password="${MUJITASK_POSTGRES_PASSWORD:-mujitask}"

  local db_url artifact_root artifact_bucket artifact_store_provider
  if [[ "${MUJITASK_RUNTIME_MODE:-native}" == "external" ]]; then
    db_url="$(config_value MUJITASK_DB_URL BUSINESS_EXECUTION_CONTROL_DB_URL "")"
  else
    db_url="$(config_value MUJITASK_DB_URL BUSINESS_EXECUTION_CONTROL_DB_URL "postgresql+psycopg://$(urlencode_component "${postgres_user}"):$(urlencode_component "${postgres_password}")@127.0.0.1:${postgres_port}/${postgres_db}")"
  fi
  if [[ -z "${db_url}" ]]; then
    fail_deploy "Missing database config. Set MUJITASK_DB_URL / BUSINESS_EXECUTION_CONTROL_DB_URL in ${ENV_FILE}."
  fi
  artifact_root="$(config_value MUJITASK_ARTIFACT_ROOT BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT "${install_dir}/runtime/execution_control/object_store")"
  artifact_bucket="$(config_value MUJITASK_MINIO_BUCKET BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET "automation-business-scaffold")"
  artifact_store_provider="$(config_value MUJITASK_ARTIFACT_STORE_PROVIDER BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER "minio")"

  local artifact_object_prefix minio_endpoint minio_access_key minio_secret_key minio_region minio_secure minio_create_bucket sync_referenced_files
  artifact_object_prefix="$(config_value MUJITASK_ARTIFACT_OBJECT_PREFIX BUSINESS_EXECUTION_CONTROL_ARTIFACT_OBJECT_PREFIX "mujitask/local")"
  minio_endpoint="$(config_value MUJITASK_MINIO_ENDPOINT BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT "127.0.0.1:${MUJITASK_MINIO_PORT:-9000}")"
  minio_access_key="$(config_value MUJITASK_MINIO_ROOT_USER BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY "minioadmin")"
  minio_secret_key="$(config_value MUJITASK_MINIO_ROOT_PASSWORD BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY "minioadmin")"
  minio_region="$(config_value MUJITASK_MINIO_REGION BUSINESS_EXECUTION_CONTROL_MINIO_REGION "")"
  minio_secure="$(config_value MUJITASK_MINIO_SECURE BUSINESS_EXECUTION_CONTROL_MINIO_SECURE "false")"
  minio_create_bucket="$(config_value MUJITASK_MINIO_CREATE_BUCKET BUSINESS_EXECUTION_CONTROL_MINIO_CREATE_BUCKET "true")"
  sync_referenced_files="$(config_value MUJITASK_SYNC_REFERENCED_FILES BUSINESS_EXECUTION_CONTROL_SYNC_REFERENCED_FILES "true")"

  local requested_by notification_channel_code openclaw_agent_id openclaw_state_dir
  requested_by="$(config_value MUJITASK_REQUESTED_BY BUSINESS_EXECUTION_CONTROL_REQUESTED_BY "${agent_type}-skill")"
  notification_channel_code="$(config_value MUJITASK_NOTIFICATION_CHANNEL_CODE NOTIFICATION_CHANNEL_CODE "feishu_bot_api")"
  openclaw_agent_id="$(config_value MUJITASK_OPENCLAW_AGENT_ID OPENCLAW_AGENT_ID "tiktok-ops")"
  openclaw_state_dir="$(config_value MUJITASK_OPENCLAW_STATE_DIR OPENCLAW_STATE_DIR "${HOME}/.openclaw")"

  prepare_project_tree "${install_dir}"
  ensure_project_install "${install_dir}"
  prepare_local_files "${install_dir}"
  start_runtime_services "${artifact_store_provider}"

  write_executor_local_env \
    "${install_dir}" \
    "${db_url}" \
    "${artifact_root}" \
    "${artifact_bucket}" \
    "${artifact_store_provider}" \
    "${artifact_object_prefix}" \
    "${minio_endpoint}" \
    "${minio_access_key}" \
    "${minio_secret_key}" \
    "${minio_region}" \
    "${minio_secure}" \
    "${minio_create_bucket}" \
    "${sync_referenced_files}" \
    "${requested_by}" \
    "${token}" \
    "${browser_profile_ref}" \
    "${fastmoss_phone}" \
    "${fastmoss_password}" \
    "${notification_channel_code}"
  merge_key_value_file \
    "${install_dir}/scripts/execution_control/executor.local.env" \
    "MUJITASK_FEISHU_ACCESS_TOKEN=$(quote_env_value "${token}")"

  wait_for_runtime \
    "${db_url}" \
    "${artifact_store_provider}" \
    "${minio_endpoint}" \
    "${minio_access_key}" \
    "${minio_secret_key}" \
    "${minio_secure}" \
    "${artifact_bucket}" \
    "${minio_create_bucket}"

  local target_skill_dir
  INSTALLED_SKILL_DIR=""
  install_agent_skill \
    "${install_dir}" \
    "${skills_dir}" \
    "${token}" \
    "${browser_profile_ref}" \
    "${fastmoss_phone}" \
    "${fastmoss_password}" \
    "${db_url}" \
    "${artifact_root}" \
    "${artifact_bucket}" \
    "${requested_by}" \
    "${notification_channel_code}" \
    "${openclaw_agent_id}" \
    "${openclaw_state_dir}" \
    "${feishu_base_url}" \
    "${tk_selection_table_id}" \
    "${tk_selection_view_id}" \
    "${tk_competitor_table_id}" \
    "${tk_competitor_view_id}" \
    "${tk_influencer_pool_table_id}" \
    "${tk_influencer_pool_view_id}" \
    "${tk_influencer_outreach_table_id}" \
    "${tk_influencer_outreach_view_id}" \
    "${tk_hot_video_table_id}" \
    "${tk_hot_video_view_id}"
  target_skill_dir="${INSTALLED_SKILL_DIR}"
  [[ -n "${target_skill_dir}" ]] || fail_deploy "Skill installation did not return a target directory."

  local resolved_ref="local-checkout"
  if command -v git >/dev/null 2>&1 && git -C "${SOURCE_DIR}" rev-parse --short HEAD >/dev/null 2>&1; then
    resolved_ref="$(git -C "${SOURCE_DIR}" rev-parse --short HEAD)"
  fi
  write_deploy_state "${install_dir}" "${MUJITASK_REPO_URL:-local-checkout}" "${resolved_ref}" "" "${LAST_FRAMEWORK_ARCHIVE_URL:-}"

  log "Installing launchd agents"
  bash "${install_dir}/scripts/execution_control/install_launch_agents.sh"

  smoke_check "${install_dir}" "${target_skill_dir}" "${install_dir}/scripts/execution_control/executor.local.env"

  log "Deployment completed."
  log "Project directory: ${install_dir}"
  log "Agent type: ${agent_type}"
  log "Skill directory: ${target_skill_dir}"
  log "Runtime mode: ${MUJITASK_RUNTIME_MODE:-native}"
}

main "$@"
