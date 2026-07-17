#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ENV_FILE="${MUJITASK_DEPLOY_ENV_FILE:-${SOURCE_DIR}/scripts/deploy/macos/deploy.local.env}"
PROJECT_DIR=""

OPENCLAW_LOG_PREFIX="mujitask-macos-deploy"
# shellcheck source=../../../examples/openclaw/openclaw_deploy_common.sh
unset TMP_ROOT
source "${SOURCE_DIR}/examples/openclaw/openclaw_deploy_common.sh"
readonly TMP_ROOT

FACT_MIGRATION_ENV_FILE_TO_CLEAN=""

cleanup_deploy_files() {
  local exit_status=$?
  if [[ -n "${FACT_MIGRATION_ENV_FILE_TO_CLEAN}" ]]; then
    rm -f -- "${FACT_MIGRATION_ENV_FILE_TO_CLEAN}" || true
  fi
  if [[ -n "${TMP_ROOT:-}" ]]; then
    rm -rf -- "${TMP_ROOT}" || true
  fi
  return "${exit_status}"
}

trap cleanup_deploy_files EXIT

# Keep managed configuration values on stdin so database URLs and passwords do
# not become visible in child-process arguments.
quote_env_value() {
  local raw_value="${1-}"
  printf '%s' "${raw_value}" | "${PYTHON_BIN}" -c \
    'import shlex, sys; print(shlex.quote(sys.stdin.read()))'
}

merge_key_value_file() {
  local file_path="$1"
  shift

  {
    printf '%s\0' "${file_path}"
    local entry
    for entry in "$@"; do
      printf '%s\0' "${entry}"
    done
  } | "${PYTHON_BIN}" -c '
from pathlib import Path
import sys

parts = sys.stdin.buffer.read().split(b"\0")
if parts and not parts[-1]:
    parts.pop()
if not parts:
    raise SystemExit("managed configuration path is required")

managed = {}
for raw_entry in parts[1:]:
    entry = raw_entry.decode("utf-8")
    key, separator, value = entry.partition("=")
    if not separator or not key:
        raise SystemExit(f"invalid managed configuration entry: {key!r}")
    managed[key] = value

path = Path(parts[0].decode("utf-8"))
path.parent.mkdir(parents=True, exist_ok=True)
lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
output_lines = []
seen_keys = set()
for raw_line in lines:
    stripped = raw_line.strip()
    if not stripped or stripped.startswith("#") or "=" not in raw_line:
        output_lines.append(raw_line)
        continue
    key = raw_line.split("=", 1)[0].strip().lstrip("\ufeff")
    if key.startswith("export "):
        key = key[len("export "):].strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in {"\"", "\047"}:
        key = key[1:-1]
    if key in managed:
        if key not in seen_keys:
            output_lines.append(f"{key}={managed[key]}\n")
            seen_keys.add(key)
        continue
    output_lines.append(raw_line)

for key, value in managed.items():
    if key not in seen_keys:
        output_lines.append(f"{key}={value}\n")

text = "".join(output_lines)
if text and not text.endswith("\n"):
    text += "\n"
path.write_text(text, encoding="utf-8")
'
}

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
  if [[ -L "${ENV_FILE}" ]]; then
    fail_deploy "Deployment environment file must not be a symlink: ${ENV_FILE}."
  fi

  local env_mode env_owner
  if [[ "$(uname -s)" == "Darwin" ]]; then
    env_mode="$(stat -f '%Lp' "${ENV_FILE}")"
    env_owner="$(stat -f '%u' "${ENV_FILE}")"
  else
    env_mode="$(stat -c '%a' "${ENV_FILE}")"
    env_owner="$(stat -c '%u' "${ENV_FILE}")"
  fi
  if [[ ! "${env_mode}" =~ ^(400|600)$ ]]; then
    fail_deploy "Deployment environment file must have mode 400 or 600: ${ENV_FILE}."
  fi
  if [[ "${env_owner}" != "$(id -u)" ]]; then
    fail_deploy "Deployment environment file must be owned by the current user: ${ENV_FILE}."
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
  local escaped
  escaped="$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e "s/'/''/g")"
  printf "E'%s'" "${escaped}"
}

sql_identifier() {
  printf "%s" "$1" | sed 's/"/""/g'
}

urlencode_component() {
  printf '%s' "$1" | "$PYTHON_BIN" -c \
    'from urllib.parse import quote; import sys; print(quote(sys.stdin.read(), safe=""))'
}

compose_postgres_url() {
  local user="$1"
  local password="$2"
  local host="$3"
  local port="$4"
  local db_name="$5"
  printf 'postgresql+psycopg://%s:%s@%s:%s/%s' \
    "$(urlencode_component "${user}")" \
    "$(urlencode_component "${password}")" \
    "${host}" \
    "${port}" \
    "${db_name}"
}

compose_local_admin_postgres_url() {
  local user="$1"
  local socket_dir="$2"
  local port="$3"
  local db_name="$4"
  printf 'postgresql+psycopg://%s@/%s?host=%s&port=%s' \
    "$(urlencode_component "${user}")" \
    "${db_name}" \
    "$(urlencode_component "${socket_dir}")" \
    "${port}"
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

  local venv_python="${install_dir}/.venv/bin/python"
  if [[ -x "${venv_python}" ]]; then
    log "Using existing project virtual environment"
  else
    log "Creating project virtual environment"
    "${UV_BIN}" venv --python 3.11 "${install_dir}/.venv" >/dev/null
  fi
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
    "${install_dir}/runtime/daemons" \
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

validate_pg_role_name() {
  local config_key="$1"
  local role_name="$2"
  if [[ ! "${role_name}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ || ${#role_name} -gt 63 ]]; then
    fail_deploy "Invalid ${config_key}=${role_name}. Use an unquoted Postgres identifier of at most 63 characters."
  fi
}

ensure_native_postgres() {
  command -v brew >/dev/null 2>&1 || fail_deploy "Homebrew is required for native macOS Postgres installation."

  local formula="${MUJITASK_POSTGRES_FORMULA:-postgresql@17}"
  local db_name="${MUJITASK_POSTGRES_DB:-automation_business_scaffold}"
  local db_user="${MUJITASK_POSTGRES_USER:-mujitask}"
  local db_password="${MUJITASK_POSTGRES_PASSWORD:-mujitask}"
  local fact_runtime_role="${MUJITASK_FACT_RUNTIME_ROLE:-}"
  local fact_runtime_password="${MUJITASK_FACT_RUNTIME_PASSWORD:-}"
  local admin_user="${MUJITASK_POSTGRES_ADMIN_USER:-$(id -un)}"
  local port="${MUJITASK_POSTGRES_PORT:-5432}"
  local socket_dir="${MUJITASK_POSTGRES_SOCKET_DIR:-/tmp}"
  validate_pg_database_name "${db_name}"
  validate_pg_role_name "MUJITASK_POSTGRES_USER" "${db_user}"
  validate_pg_role_name "MUJITASK_FACT_RUNTIME_ROLE" "${fact_runtime_role}"
  [[ -n "${fact_runtime_password}" ]] || fail_deploy "Missing MUJITASK_FACT_RUNTIME_PASSWORD in ${ENV_FILE}."
  [[ "${admin_user}" != "${fact_runtime_role}" ]] || fail_deploy "MUJITASK_POSTGRES_ADMIN_USER must differ from MUJITASK_FACT_RUNTIME_ROLE."
  [[ "${fact_runtime_role}" != "${db_user}" ]] || fail_deploy "MUJITASK_FACT_RUNTIME_ROLE must differ from MUJITASK_POSTGRES_USER."

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

  local fact_runtime_role_sql fact_runtime_password_sql fact_runtime_role_ident
  fact_runtime_role_sql="$(sql_literal "${fact_runtime_role}")"
  fact_runtime_password_sql="$(sql_literal "${fact_runtime_password}")"
  fact_runtime_role_ident="$(sql_identifier "${fact_runtime_role}")"

  if PGHOST="${socket_dir}" PGPORT="${port}" PGUSER="${admin_user}" "${psql_bin}" \
    -d postgres \
    -Atqc "SELECT 1 FROM pg_roles WHERE rolname = ${db_user_sql}" | grep -q '^1$'; then
    log "Postgres role already exists: ${db_user}"
    PGHOST="${socket_dir}" PGPORT="${port}" PGUSER="${admin_user}" "${psql_bin}" \
      -d postgres \
      -v ON_ERROR_STOP=1 >/dev/null <<SQL
ALTER ROLE "${db_user_ident}" WITH LOGIN PASSWORD ${db_password_sql};
SQL
  else
    log "Creating Postgres role: ${db_user}"
    PGHOST="${socket_dir}" PGPORT="${port}" PGUSER="${admin_user}" "${psql_bin}" \
      -d postgres \
      -v ON_ERROR_STOP=1 >/dev/null <<SQL
CREATE ROLE "${db_user_ident}" WITH LOGIN PASSWORD ${db_password_sql};
SQL
  fi

  if PGHOST="${socket_dir}" PGPORT="${port}" PGUSER="${admin_user}" "${psql_bin}" \
    -d postgres \
    -Atqc "SELECT 1 FROM pg_roles WHERE rolname = ${fact_runtime_role_sql}" | grep -q '^1$'; then
    log "Fact runtime role already exists: ${fact_runtime_role}"
    PGHOST="${socket_dir}" PGPORT="${port}" PGUSER="${admin_user}" "${psql_bin}" \
      -d postgres \
      -v ON_ERROR_STOP=1 >/dev/null <<SQL
ALTER ROLE "${fact_runtime_role_ident}" WITH LOGIN PASSWORD ${fact_runtime_password_sql} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
SQL
  else
    log "Creating restricted Fact runtime role: ${fact_runtime_role}"
    PGHOST="${socket_dir}" PGPORT="${port}" PGUSER="${admin_user}" "${psql_bin}" \
      -d postgres \
      -v ON_ERROR_STOP=1 >/dev/null <<SQL
CREATE ROLE "${fact_runtime_role_ident}" WITH LOGIN PASSWORD ${fact_runtime_password_sql} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
SQL
  fi

  if PGHOST="${socket_dir}" PGPORT="${port}" PGUSER="${admin_user}" "${psql_bin}" \
    -d postgres \
    -Atqc "SELECT 1 FROM pg_database WHERE datname = ${db_name_sql}" | grep -q '^1$'; then
    log "Postgres database already exists: ${db_name}"
  else
    log "Creating Postgres database: ${db_name} owned by ${db_user}"
    PGHOST="${socket_dir}" PGPORT="${port}" PGUSER="${admin_user}" "${createdb_bin}" -O "${db_user}" "${db_name}"
  fi

  PGHOST="${socket_dir}" PGPORT="${port}" PGUSER="${admin_user}" "${psql_bin}" \
    -d "${db_name}" \
    -v ON_ERROR_STOP=1 \
    -c "GRANT ALL PRIVILEGES ON DATABASE \"${db_name_ident}\" TO \"${db_user_ident}\"" \
    -c "GRANT CONNECT ON DATABASE \"${db_name_ident}\" TO \"${fact_runtime_role_ident}\"" \
    -c "GRANT ALL ON SCHEMA public TO \"${db_user_ident}\"" \
    -c "REVOKE ALL ON SCHEMA public FROM \"${fact_runtime_role_ident}\"" \
    -c "REVOKE CREATE ON SCHEMA public FROM PUBLIC" \
    -c "GRANT USAGE ON SCHEMA public TO \"${fact_runtime_role_ident}\"" >/dev/null
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

  validate_private_file_target "${plist_path}" "MinIO launchd plist"
  if ! launchctl print "gui/${uid_value}/${label}" >/dev/null 2>&1; then
    if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
      log "MinIO API port ${port} is already listening; using the existing local MinIO service."
      return 0
    fi
  fi

  mkdir -p "${launch_agents_dir}" "${log_dir}" "${data_dir}"

  local previous_umask
  previous_umask="$(umask)"
  umask 077
  log "Installing MinIO launchd service"
  MUJITASK_DEPLOY_MINIO_ROOT_USER="${root_user}" \
  MUJITASK_DEPLOY_MINIO_ROOT_PASSWORD="${root_password}" \
    "${PYTHON_BIN}" - \
    "${plist_path}" \
    "${label}" \
    "${minio_bin}" \
    "${data_dir}" \
    "${bind}:${port}" \
    "${console_bind}:${console_port}" \
    "${PROJECT_DIR}" \
    "${log_dir}/minio.stdout.log" \
    "${log_dir}/minio.stderr.log" <<'PY'
from pathlib import Path
import os
import plistlib
import sys

(
    plist_path,
    label,
    minio_bin,
    data_dir,
    address,
    console_address,
    working_dir,
    stdout_path,
    stderr_path,
) = sys.argv[1:]
root_user = os.environ.pop("MUJITASK_DEPLOY_MINIO_ROOT_USER")
root_password = os.environ.pop("MUJITASK_DEPLOY_MINIO_ROOT_PASSWORD")

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
  umask "${previous_umask}"
  seal_private_file "${plist_path}" "MinIO launchd plist"

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
  MUJITASK_DEPLOY_RUNTIME_DB_URL="${db_url}" \
  MUJITASK_DEPLOY_ARTIFACT_PROVIDER="${provider}" \
  MUJITASK_DEPLOY_MINIO_ENDPOINT="${endpoint}" \
  MUJITASK_DEPLOY_MINIO_ACCESS_KEY="${access_key}" \
  MUJITASK_DEPLOY_MINIO_SECRET_KEY="${secret_key}" \
  MUJITASK_DEPLOY_MINIO_SECURE="${secure}" \
  MUJITASK_DEPLOY_ARTIFACT_BUCKET="${bucket}" \
  MUJITASK_DEPLOY_MINIO_CREATE_BUCKET="${create_bucket}" \
    "${venv_python}" - <<'PY'
import os
import time

from sqlalchemy import create_engine, text

db_url = os.environ.pop("MUJITASK_DEPLOY_RUNTIME_DB_URL")
provider = os.environ.pop("MUJITASK_DEPLOY_ARTIFACT_PROVIDER")
endpoint = os.environ.pop("MUJITASK_DEPLOY_MINIO_ENDPOINT")
access_key = os.environ.pop("MUJITASK_DEPLOY_MINIO_ACCESS_KEY")
secret_key = os.environ.pop("MUJITASK_DEPLOY_MINIO_SECRET_KEY")
secure = os.environ.pop("MUJITASK_DEPLOY_MINIO_SECURE")
bucket = os.environ.pop("MUJITASK_DEPLOY_ARTIFACT_BUCKET")
create_bucket = os.environ.pop("MUJITASK_DEPLOY_MINIO_CREATE_BUCKET")
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

validate_private_file_target() {
  local file_path="$1"
  local file_label="$2"
  [[ ! -L "${file_path}" ]] || fail_deploy "${file_label} must not be a symlink: ${file_path}."
  if [[ -e "${file_path}" ]]; then
    [[ -f "${file_path}" ]] || fail_deploy "${file_label} path must be a regular file: ${file_path}."
    local file_owner
    if [[ "$(uname -s)" == "Darwin" ]]; then
      file_owner="$(stat -f '%u' "${file_path}")"
    else
      file_owner="$(stat -c '%u' "${file_path}")"
    fi
    [[ "${file_owner}" == "$(id -u)" ]] || fail_deploy "${file_label} must be owned by the current user: ${file_path}."
    chmod 600 "${file_path}"
  fi
}

seal_private_file() {
  local file_path="$1"
  local file_label="$2"
  [[ -f "${file_path}" && ! -L "${file_path}" ]] || fail_deploy "${file_label} must be a regular, non-symlink file: ${file_path}."

  local file_owner file_mode
  if [[ "$(uname -s)" == "Darwin" ]]; then
    file_owner="$(stat -f '%u' "${file_path}")"
  else
    file_owner="$(stat -c '%u' "${file_path}")"
  fi
  [[ "${file_owner}" == "$(id -u)" ]] || fail_deploy "${file_label} must be owned by the current user: ${file_path}."

  chmod 600 "${file_path}"
  if [[ "$(uname -s)" == "Darwin" ]]; then
    file_mode="$(stat -f '%Lp' "${file_path}")"
    file_owner="$(stat -f '%u' "${file_path}")"
  else
    file_mode="$(stat -c '%a' "${file_path}")"
    file_owner="$(stat -c '%u' "${file_path}")"
  fi
  [[ "${file_mode}" == "600" ]] || fail_deploy "${file_label} must have mode 600: ${file_path}."
  [[ "${file_owner}" == "$(id -u)" ]] || fail_deploy "${file_label} ownership changed unexpectedly: ${file_path}."
}

write_fact_migration_env() {
  local env_file="$1"
  local fact_migration_db_url="$2"
  local fact_runtime_role="$3"
  local temp_file

  mkdir -p "$(dirname "${env_file}")"
  [[ ! -L "${env_file}" ]] || fail_deploy "Fact migration environment file must not be a symlink: ${env_file}."
  temp_file="$(mktemp "${env_file}.tmp.XXXXXX")"
  FACT_MIGRATION_ENV_FILE_TO_CLEAN="${temp_file}"
  chmod 600 "${temp_file}"
  {
    printf 'BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL=%s\n' "$(quote_env_value "${fact_migration_db_url}")"
    printf 'BUSINESS_EXECUTION_CONTROL_FACT_RUNTIME_ROLE=%s\n' "$(quote_env_value "${fact_runtime_role}")"
  } >"${temp_file}"
  mv -f "${temp_file}" "${env_file}"
  FACT_MIGRATION_ENV_FILE_TO_CLEAN="${env_file}"
  chmod 600 "${env_file}"
}

verify_database_identity() {
  local database_label="$1"
  local worker_db_url="$2"
  local migration_db_url="$3"
  local venv_python="${PROJECT_DIR}/.venv/bin/python"

  log "Verifying ${database_label} worker and migration database identity"
  MUJITASK_DEPLOY_DATABASE_LABEL="${database_label}" \
  MUJITASK_DEPLOY_WORKER_DB_URL="${worker_db_url}" \
  MUJITASK_DEPLOY_MIGRATION_DB_URL="${migration_db_url}" \
    "${venv_python}" - <<'PY'
import os

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool


def database_identity(db_url: str) -> tuple[str, str, str, str]:
    engine = create_engine(db_url, future=True, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT current_database() AS database_name, "
                    "(SELECT oid::text FROM pg_database "
                    " WHERE datname = current_database()) AS database_oid, "
                    "pg_postmaster_start_time()::text AS postmaster_started_at, "
                    "current_setting('server_version_num') AS server_version_num"
                )
            ).mappings().one()
            return tuple(str(row[key]) for key in row)
    finally:
        engine.dispose()


database_label = os.environ.pop("MUJITASK_DEPLOY_DATABASE_LABEL")
worker_db_url = os.environ.pop("MUJITASK_DEPLOY_WORKER_DB_URL")
migration_db_url = os.environ.pop("MUJITASK_DEPLOY_MIGRATION_DB_URL")
if database_identity(worker_db_url) != database_identity(migration_db_url):
    raise SystemExit(
        f"{database_label} worker and migration URLs must resolve to the same running "
        "PostgreSQL instance and database."
    )

print(f"{database_label.lower()}_database_identity_ready")
PY
}

bootstrap_native_legacy_schemas() {
  local runtime_db_url="$1"
  local venv_python="${PROJECT_DIR}/.venv/bin/python"

  log "Bootstrapping native Runtime and TikTok Fact schemas"
  MUJITASK_DEPLOY_RUNTIME_DB_URL="${runtime_db_url}" "${venv_python}" - <<'PY'
import os

from automation_business_scaffold.infrastructure.facts.tk_fact_store import TKFactStore
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

runtime_store = RuntimeStore(db_url=os.environ.pop("MUJITASK_DEPLOY_RUNTIME_DB_URL"))
runtime_store.bootstrap_schema()
TKFactStore(runtime_store=runtime_store).bootstrap_schema()
print("native_legacy_schema_ready")
PY
}

grant_native_fact_runtime_compatibility() {
  local migration_db_url="$1"
  local fact_runtime_role="$2"
  local venv_python="${PROJECT_DIR}/.venv/bin/python"

  MUJITASK_DEPLOY_FACT_MIGRATION_DB_URL="${migration_db_url}" \
  MUJITASK_DEPLOY_FACT_RUNTIME_ROLE="${fact_runtime_role}" \
    "${venv_python}" - <<'PY'
import os
import re

from sqlalchemy import create_engine, inspect, text

from automation_business_scaffold.infrastructure.schemas.amazon_fact_schema import (
    AMAZON_FACT_TABLES,
    AMAZON_FACT_VERSION_TABLE,
)
from automation_business_scaffold.infrastructure.schemas.fact_schema import (
    TK_FACT_SCHEMA_STATEMENTS,
)

TK_FACT_TABLES = tuple(
    match.group(1)
    for statement in TK_FACT_SCHEMA_STATEMENTS
    if (match := re.search(r"\bCREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(tk_[a-z0-9_]+)\b", statement))
)

db_url = os.environ.pop("MUJITASK_DEPLOY_FACT_MIGRATION_DB_URL")
role = os.environ.pop("MUJITASK_DEPLOY_FACT_RUNTIME_ROLE")
if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", role) is None:
    raise SystemExit("invalid Fact runtime role")

engine = create_engine(db_url, future=True)
try:
    with engine.begin() as connection:
        schema = str(connection.dialect.default_schema_name or "public")
        preparer = connection.dialect.identifier_preparer
        quoted_schema = preparer.quote_identifier(schema)
        quoted_role = preparer.quote_identifier(role)
        available_tables = set(inspect(connection).get_table_names(schema=schema))
        governed_tables = [*TK_FACT_TABLES, *AMAZON_FACT_TABLES]
        missing_tables = sorted(set(governed_tables) - available_tables)
        if missing_tables:
            raise SystemExit(
                "Fact database is missing governed tables: " + ", ".join(missing_tables)
            )
        if AMAZON_FACT_VERSION_TABLE not in available_tables:
            raise SystemExit("Fact migration version table is missing.")
        user_schemas = connection.execute(
            text(
                "SELECT nspname FROM pg_namespace "
                "WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'"
            )
        ).scalars().all()
        for user_schema in user_schemas:
            quoted_user_schema = preparer.quote_identifier(user_schema)
            connection.exec_driver_sql(
                "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA "
                f"{quoted_user_schema} FROM {quoted_role}"
            )
            connection.exec_driver_sql(
                "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA "
                f"{quoted_user_schema} FROM {quoted_role}"
            )
            connection.exec_driver_sql(
                f"REVOKE ALL PRIVILEGES ON SCHEMA {quoted_user_schema} FROM {quoted_role}"
            )
        connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA {quoted_schema} TO {quoted_role}")
        qualified = ", ".join(
            f"{quoted_schema}.{preparer.quote_identifier(name)}"
            for name in governed_tables
        )
        connection.exec_driver_sql(
            f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM {quoted_role}"
        )
        connection.exec_driver_sql(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {qualified} TO {quoted_role}"
        )
        version_table = (
            f"{quoted_schema}.{preparer.quote_identifier(AMAZON_FACT_VERSION_TABLE)}"
        )
        connection.exec_driver_sql(
            f"REVOKE ALL PRIVILEGES ON TABLE {version_table} FROM {quoted_role}"
        )
        connection.exec_driver_sql(
            f"GRANT SELECT ON TABLE {version_table} TO {quoted_role}"
        )
finally:
    engine.dispose()
PY
}

verify_fact_runtime_privileges() {
  local fact_db_url="$1"
  local fact_runtime_role="$2"
  local venv_python="${PROJECT_DIR}/.venv/bin/python"

  log "Verifying restricted Fact runtime privileges"
  MUJITASK_DEPLOY_FACT_DB_URL="${fact_db_url}" \
  MUJITASK_DEPLOY_FACT_RUNTIME_ROLE="${fact_runtime_role}" \
    "${venv_python}" - <<'PY'
import os
import re

from sqlalchemy import create_engine, inspect, text

from automation_business_scaffold.infrastructure.schemas.amazon_fact_schema import (
    AMAZON_FACT_SCHEMA_REVISION,
    AMAZON_FACT_TABLES,
    AMAZON_FACT_VERSION_TABLE,
)
from automation_business_scaffold.infrastructure.schemas.fact_schema import (
    TK_FACT_SCHEMA_STATEMENTS,
)

TK_FACT_TABLES = tuple(
    match.group(1)
    for statement in TK_FACT_SCHEMA_STATEMENTS
    if (match := re.search(r"\bCREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(tk_[a-z0-9_]+)\b", statement))
)
BASE_TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
COLUMN_PRIVILEGES = {"SELECT", "INSERT", "UPDATE", "REFERENCES"}

db_url = os.environ.pop("MUJITASK_DEPLOY_FACT_DB_URL")
expected_role = os.environ.pop("MUJITASK_DEPLOY_FACT_RUNTIME_ROLE")
engine = create_engine(db_url, future=True, pool_pre_ping=True)
try:
    with engine.connect() as connection:
        schema = str(connection.dialect.default_schema_name or "public")
        preparer = connection.dialect.identifier_preparer
        role_row = connection.execute(
            text(
                "SELECT current_user AS role_name, rolcanlogin, rolsuper, "
                "rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
        ).mappings().one()
        if role_row["role_name"] != expected_role:
            raise SystemExit(
                "Fact runtime URL authenticated as "
                f"{role_row['role_name']!r}, expected {expected_role!r}."
            )
        if not bool(role_row["rolcanlogin"]):
            raise SystemExit("Fact runtime role cannot log in.")
        if any(
            bool(role_row[key])
            for key in (
                "rolsuper",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "rolbypassrls",
            )
        ):
            raise SystemExit("Fact runtime role has elevated Postgres role attributes.")
        memberships = connection.execute(
            text(
                "SELECT parent.rolname FROM pg_auth_members AS member "
                "JOIN pg_roles AS parent ON parent.oid = member.roleid "
                "WHERE member.member = (SELECT oid FROM pg_roles WHERE rolname = current_user)"
            )
        ).scalars().all()
        if memberships:
            raise SystemExit(
                "Fact runtime role retains role memberships: " + ", ".join(memberships)
            )
        if connection.execute(
            text(
                "SELECT has_database_privilege("
                "current_user, current_database(), 'CREATE')"
            )
        ).scalar_one():
            raise SystemExit("Fact runtime role has CREATE privilege on the Fact database.")
        if connection.execute(
            text(
                "SELECT pg_get_userbyid(datdba) = current_user "
                "FROM pg_database WHERE datname = current_database()"
            )
        ).scalar_one():
            raise SystemExit("Fact runtime role owns the Fact database.")

        schema_rows = connection.execute(
            text(
                "SELECT nspname AS schema_name, pg_get_userbyid(nspowner) AS owner_name "
                "FROM pg_namespace "
                "WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' "
                "ORDER BY nspname"
            )
        ).mappings().all()
        schema_names = {row["schema_name"] for row in schema_rows}
        if schema not in schema_names:
            raise SystemExit(f"Fact schema is missing: {schema}.")
        unexpected_schema_privileges = []
        for row in schema_rows:
            schema_name = row["schema_name"]
            if row["owner_name"] == expected_role:
                raise SystemExit(f"Fact runtime role owns schema: {schema_name}.")
            granted = [
                privilege
                for privilege in ("USAGE", "CREATE")
                if connection.execute(
                    text(
                        "SELECT has_schema_privilege("
                        "current_user, :schema_name, :privilege)"
                    ),
                    {"schema_name": schema_name, "privilege": privilege},
                ).scalar_one()
            ]
            if schema_name == schema:
                if "USAGE" not in granted:
                    raise SystemExit("Fact runtime role lacks USAGE on the Fact schema.")
                if "CREATE" in granted:
                    raise SystemExit("Fact runtime role has CREATE on the Fact schema.")
            elif granted:
                unexpected_schema_privileges.append(
                    f"{schema_name} ({', '.join(granted)})"
                )
        if unexpected_schema_privileges:
            raise SystemExit(
                "Fact runtime role has privileges outside the Fact schema: "
                + ", ".join(unexpected_schema_privileges)
            )

        relation_rows = connection.execute(
            text(
                "SELECT c.oid AS relation_oid, n.nspname AS schema_name, "
                "c.relname AS relation_name, c.relkind, "
                "pg_get_userbyid(c.relowner) AS owner_name "
                "FROM pg_class AS c "
                "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE n.nspname !~ '^pg_' "
                "AND n.nspname <> 'information_schema' "
                "AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') "
                "ORDER BY n.nspname, c.relname"
            )
        ).mappings().all()
        relations = {
            (row["schema_name"], row["relation_name"]): row
            for row in relation_rows
        }
        default_schema_relations = {
            relation_name
            for relation_schema, relation_name in relations
            if relation_schema == schema
        }
        missing_amazon_tables = sorted(
            set(AMAZON_FACT_TABLES) - default_schema_relations
        )
        if missing_amazon_tables:
            raise SystemExit(
                "Fact migration is missing governed Amazon tables: "
                + ", ".join(missing_amazon_tables)
            )
        missing_tk_tables = sorted(set(TK_FACT_TABLES) - default_schema_relations)
        if missing_tk_tables:
            raise SystemExit(
                "Fact database is missing governed TikTok tables: "
                + ", ".join(missing_tk_tables)
            )
        if AMAZON_FACT_VERSION_TABLE not in default_schema_relations:
            raise SystemExit("Fact migration version table is missing.")
        governed_tables = [*AMAZON_FACT_TABLES, *TK_FACT_TABLES, AMAZON_FACT_VERSION_TABLE]
        owned_relations = [
            f"{row['schema_name']}.{row['relation_name']}"
            for row in relation_rows
            if row["owner_name"] == expected_role
        ]
        if owned_relations:
            raise SystemExit(
                "Fact runtime role owns schema relations: "
                + ", ".join(owned_relations)
            )

        def qualified(table_name: str) -> str:
            return (
                f"{preparer.quote_identifier(schema)}."
                f"{preparer.quote_identifier(table_name)}"
            )

        server_version_num = int(
            connection.execute(text("SHOW server_version_num")).scalar_one()
        )
        table_privileges = list(BASE_TABLE_PRIVILEGES)
        if server_version_num >= 170000:
            table_privileges.append("MAINTAIN")

        def relation_has_privilege(row, privilege: str) -> bool:
            if row["relkind"] == "S":
                return bool(
                    connection.execute(
                        text(
                            "SELECT has_sequence_privilege("
                            "current_user, CAST(:relation_oid AS oid), :privilege)"
                        ),
                        {
                            "relation_oid": row["relation_oid"],
                            "privilege": privilege,
                        },
                    ).scalar_one()
                )
            privilege_function = (
                "has_any_column_privilege"
                if privilege in COLUMN_PRIVILEGES
                else "has_table_privilege"
            )
            return bool(
                connection.execute(
                    text(
                        f"SELECT {privilege_function}("
                        "current_user, CAST(:relation_oid AS oid), :privilege)"
                    ),
                    {
                        "relation_oid": row["relation_oid"],
                        "privilege": privilege,
                    },
                ).scalar_one()
            )

        def table_has_privilege(row, privilege: str) -> bool:
            return bool(
                connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "current_user, CAST(:relation_oid AS oid), :privilege)"
                    ),
                    {
                        "relation_oid": row["relation_oid"],
                        "privilege": privilege,
                    },
                ).scalar_one()
            )

        allowed_privileges = {
            (schema, table_name): {"SELECT", "INSERT", "UPDATE", "DELETE"}
            for table_name in [*AMAZON_FACT_TABLES, *TK_FACT_TABLES]
        }
        allowed_privileges[(schema, AMAZON_FACT_VERSION_TABLE)] = {"SELECT"}
        for relation_key in allowed_privileges:
            row = relations[relation_key]
            if row["relkind"] not in {"r", "p"}:
                raise SystemExit(
                    "Governed Fact relation must be a table: "
                    f"{row['schema_name']}.{row['relation_name']}."
                )
        unexpected_table_privileges = []
        for row in relation_rows:
            relation_key = (row["schema_name"], row["relation_name"])
            if row["relkind"] == "S":
                granted = {
                    privilege
                    for privilege in ("USAGE", "SELECT", "UPDATE")
                    if relation_has_privilege(row, privilege)
                }
            else:
                granted = {
                    privilege
                    for privilege in table_privileges
                    if relation_has_privilege(row, privilege)
                }
            expected = allowed_privileges.get(relation_key, set())
            missing = {
                privilege
                for privilege in expected
                if not table_has_privilege(row, privilege)
            }
            if missing:
                raise SystemExit(
                    "Fact runtime role lacks "
                    f"{', '.join(sorted(missing))} on "
                    f"{row['schema_name']}.{row['relation_name']}."
                )
            forbidden = granted - expected
            if forbidden:
                unexpected_table_privileges.append(
                    f"{row['schema_name']}.{row['relation_name']} "
                    f"({', '.join(sorted(forbidden))})"
                )
        if unexpected_table_privileges:
            raise SystemExit(
                "Fact runtime role has privileges outside governed Fact tables: "
                + ", ".join(unexpected_table_privileges)
            )

        version_table = qualified(AMAZON_FACT_VERSION_TABLE)
        actual_revision = connection.execute(
            text(f"SELECT version_num FROM {version_table}")
        ).scalar_one_or_none()
        if actual_revision != AMAZON_FACT_SCHEMA_REVISION:
            raise SystemExit(
                "Fact schema revision mismatch: "
                f"expected {AMAZON_FACT_SCHEMA_REVISION}, got {actual_revision!r}."
            )
finally:
    engine.dispose()

print("fact_runtime_privileges_ready")
PY
}

install_external_launch_agents() {
  local install_dir="$1"
  local launch_agents_dir="${HOME}/Library/LaunchAgents"
  local template_dir="${install_dir}/config/deployment/launchd"
  local log_dir="${install_dir}/runtime/daemons"
  local uid_value
  uid_value="$(id -u)"
  local labels=(
    "com.happyzhao.mujitask.executor-daemon"
    "com.happyzhao.mujitask.api-worker"
    "com.happyzhao.mujitask.browser-runloop"
    "com.happyzhao.mujitask.outbox-dispatcher"
    "com.happyzhao.mujitask.watchdog"
  )

  mkdir -p "${launch_agents_dir}" "${log_dir}"
  chmod +x "${install_dir}/scripts/execution_control/run_launchd_agent.sh"

  "${PYTHON_BIN}" - "${install_dir}" "${template_dir}" "${launch_agents_dir}" <<'PY'
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

  pkill -f 'automation_business_scaffold.apps.daemons.executor.main' >/dev/null 2>&1 || true
  pkill -f 'automation_business_scaffold.apps.daemons.api_worker.main' >/dev/null 2>&1 || true
  pkill -f 'automation_business_scaffold.apps.daemons.browser_worker.main' >/dev/null 2>&1 || true
  pkill -f 'automation_business_scaffold.apps.daemons.outbox.main' >/dev/null 2>&1 || true
  pkill -f 'automation_business_scaffold.apps.daemons.watchdog.main' >/dev/null 2>&1 || true

  sleep 1

  local label plist_path
  for label in "${labels[@]}"; do
    plist_path="${launch_agents_dir}/${label}.plist"
    launchctl bootout "gui/${uid_value}" "${plist_path}" >/dev/null 2>&1 || true
  done
  for label in "${labels[@]}"; do
    plist_path="${launch_agents_dir}/${label}.plist"
    launchctl bootstrap "gui/${uid_value}" "${plist_path}"
    launchctl kickstart -k "gui/${uid_value}/${label}"
  done

  launchctl list | grep 'com.happyzhao.mujitask' || true
}

install_agent_skill() {
  local install_dir="$1"
  local skills_dir="$2"
  local token="$3"
  local fastmoss_phone="$4"
  local fastmoss_password="$5"
  local notification_channel_code="$6"
  local openclaw_agent_id="$7"
  local openclaw_state_dir="$8"
  local feishu_base_url="$9"
  local tk_selection_table_id="${10}"
  local tk_selection_view_id="${11}"
  local tk_competitor_table_id="${12}"
  local tk_competitor_view_id="${13}"
  local tk_influencer_pool_table_id="${14}"
  local tk_influencer_pool_view_id="${15}"
  local tk_influencer_outreach_table_id="${16}"
  local tk_influencer_outreach_view_id="${17}"
  local tk_hot_video_table_id="${18}"
  local tk_hot_video_view_id="${19}"

  local source_skill_dir="${install_dir}/skills/mujitask-tiktok-feishu-sync"
  local target_skill_dir="${skills_dir}/mujitask-tiktok-feishu-sync"
  local skill_env_file="${target_skill_dir}/skill.local.env"

  [[ -d "${source_skill_dir}" ]] || fail_deploy "Missing skill bundle at ${source_skill_dir}."
  validate_private_file_target "${skill_env_file}" "Skill environment file"
  local previous_umask
  previous_umask="$(umask)"
  umask 077

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

  validate_private_file_target "${skill_env_file}" "Skill environment file"
  seed_key_value_file_from_example "${skill_env_file}" "${target_skill_dir}/skill.local.env.example"
  remove_skill_runtime_config_keys "${skill_env_file}"
  remove_key_value_file \
    "${skill_env_file}" \
    "AMAZON_US_BROWSER_PROFILE_REF" \
    "MUJITASK_AMAZON_US_BROWSER_PROFILE_REF" \
    "BUSINESS_EXECUTION_CONTROL_MIGRATION_ENV_FILE" \
    "BUSINESS_EXECUTION_CONTROL_MIGRATION_DB_URL" \
    "BUSINESS_EXECUTION_CONTROL_RUNTIME_MIGRATION_DB_URL" \
    "BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL" \
    "BUSINESS_EXECUTION_CONTROL_FACT_RUNTIME_ROLE" \
    "MUJITASK_RUNTIME_MIGRATION_DB_URL" \
    "MUJITASK_FACT_MIGRATION_DB_URL" \
    "MUJITASK_FACT_RUNTIME_ROLE" \
    "MUJITASK_FACT_RUNTIME_PASSWORD" \
    "MUJITASK_FEISHU_AMAZON_PRODUCTS_BASE_URL" \
    "MUJITASK_FEISHU_AMAZON_PRODUCTS_TABLE_ID" \
    "MUJITASK_FEISHU_AMAZON_PRODUCTS_VIEW_ID" \
    "MUJITASK_FEISHU_AMAZON_PRODUCTS_ACCESS_TOKEN"
  merge_key_value_file \
    "${skill_env_file}" \
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
    "FASTMOSS_PHONE=$(quote_env_value "${fastmoss_phone}")" \
    "FASTMOSS_PASSWORD=$(quote_env_value "${fastmoss_password}")" \
    "NOTIFICATION_CHANNEL_CODE=$(quote_env_value "${notification_channel_code}")" \
    "OPENCLAW_AGENT_ID=$(quote_env_value "${openclaw_agent_id}")" \
    "OPENCLAW_STATE_DIR=$(quote_env_value "${openclaw_state_dir}")"
  umask "${previous_umask}"
  seal_private_file "${skill_env_file}" "Skill environment file"
  INSTALLED_SKILL_DIR="${target_skill_dir}"
}

install_amazon_agent_skill() {
  local install_dir="$1"
  local skills_dir="$2"
  local notification_channel_code="$3"
  local openclaw_agent_id="$4"
  local openclaw_state_dir="$5"
  local feishu_account_id="$6"
  local feishu_base_url="$7"
  local table_id="$8"
  local view_id="$9"

  local source_skill_dir="${install_dir}/skills/mujitask-amazon-feishu-sync"
  local target_skill_dir="${skills_dir}/mujitask-amazon-feishu-sync"
  local skill_env_file="${target_skill_dir}/skill.local.env"

  [[ -d "${source_skill_dir}" ]] || fail_deploy "Missing Amazon skill bundle at ${source_skill_dir}."
  validate_private_file_target "${skill_env_file}" "Amazon skill environment file"
  local previous_umask
  previous_umask="$(umask)"
  umask 077

  mkdir -p "${skills_dir}"
  local previous_skill_env="${TMP_ROOT}/previous-amazon-skill.local.env"
  if [[ -f "${target_skill_dir}/skill.local.env" ]]; then
    cp "${target_skill_dir}/skill.local.env" "${previous_skill_env}"
  fi
  replace_target_dir "${target_skill_dir}"
  cp -R "${source_skill_dir}"/. "${target_skill_dir}"/
  if [[ -f "${previous_skill_env}" ]]; then
    cp "${previous_skill_env}" "${skill_env_file}"
  fi

  validate_private_file_target "${skill_env_file}" "Amazon skill environment file"
  seed_key_value_file_from_example "${skill_env_file}" "${target_skill_dir}/skill.local.env.example"
  remove_skill_runtime_config_keys "${skill_env_file}"
  merge_key_value_file \
    "${skill_env_file}" \
    "INSTALL_DIR=$(quote_env_value "${install_dir}")" \
    "NOTIFICATION_CHANNEL_CODE=$(quote_env_value "${notification_channel_code}")" \
    "OPENCLAW_AGENT_ID=$(quote_env_value "${openclaw_agent_id}")" \
    "OPENCLAW_STATE_DIR=$(quote_env_value "${openclaw_state_dir}")" \
    "OPENCLAW_DELIVERY_ACCOUNT_ID=$(quote_env_value "${feishu_account_id}")" \
    "MUJITASK_FEISHU_AMAZON_PRODUCTS_BASE_URL=$(quote_env_value "${feishu_base_url}")" \
    "MUJITASK_FEISHU_AMAZON_PRODUCTS_TABLE_ID=$(quote_env_value "${table_id}")" \
    "MUJITASK_FEISHU_AMAZON_PRODUCTS_VIEW_ID=$(quote_env_value "${view_id}")"
  umask "${previous_umask}"
  seal_private_file "${skill_env_file}" "Amazon skill environment file"
  INSTALLED_AMAZON_SKILL_DIR="${target_skill_dir}"
}

main() {
  load_deploy_env
  FACT_MIGRATION_ENV_FILE_TO_CLEAN=""
  local configured_runtime_migration_db_url="${MUJITASK_RUNTIME_MIGRATION_DB_URL:-}"
  local configured_fact_migration_db_url="${MUJITASK_FACT_MIGRATION_DB_URL:-}"
  unset MUJITASK_RUNTIME_MIGRATION_DB_URL \
    MUJITASK_FACT_MIGRATION_DB_URL \
    BUSINESS_EXECUTION_CONTROL_RUNTIME_MIGRATION_DB_URL \
    BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL
  MUJITASK_PREFLIGHT_REQUIRE_ENV=1 MUJITASK_DEPLOY_ENV_FILE="${ENV_FILE}" bash "${SOURCE_DIR}/scripts/deploy/macos/preflight.sh"

  local install_dir tiktok_skills_dir amazon_skills_dir agent_type
  install_dir="$(resolve_path "$(config_value MUJITASK_INSTALL_DIR INSTALL_DIR "${HOME}/apps/mujitask")" "${SOURCE_DIR}")"
  tiktok_skills_dir="$(resolve_path "$(require_config_value MUJITASK_TIKTOK_SKILLS_DIR MUJITASK_SKILLS_DIR)" "${HOME}")"
  amazon_skills_dir="$(resolve_path "$(require_config_value MUJITASK_AMAZON_SKILLS_DIR)" "${HOME}")"
  [[ "${tiktok_skills_dir}" != "${amazon_skills_dir}" ]] || fail_deploy "TikTok and Amazon skills directories must be different."
  agent_type="$(config_value MUJITASK_AGENT_TYPE AGENT_TYPE "generic")"
  PROJECT_DIR="${install_dir}"

  local token browser_profile_ref amazon_us_browser_profile_ref fastmoss_phone fastmoss_password
  token="$(require_config_value MUJITASK_FEISHU_ACCESS_TOKEN)"
  browser_profile_ref="$(config_value MUJITASK_BROWSER_PROFILE_REF BROWSER_PROFILE_REF "roxy-tiktok")"
  amazon_us_browser_profile_ref="$(require_config_value MUJITASK_AMAZON_US_BROWSER_PROFILE_REF)"
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
  local amazon_products_base_url amazon_products_table_id amazon_products_view_id amazon_products_access_token
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
  amazon_products_base_url="$(require_config_value MUJITASK_FEISHU_AMAZON_PRODUCTS_BASE_URL)"
  amazon_products_table_id="$(require_config_value MUJITASK_FEISHU_AMAZON_PRODUCTS_TABLE_ID)"
  amazon_products_view_id="$(require_config_value MUJITASK_FEISHU_AMAZON_PRODUCTS_VIEW_ID)"
  amazon_products_access_token="$(config_value MUJITASK_FEISHU_AMAZON_PRODUCTS_ACCESS_TOKEN MUJITASK_FEISHU_ACCESS_TOKEN "${token}")"

  local postgres_port postgres_db postgres_user postgres_password postgres_admin_user postgres_socket_dir
  postgres_port="${MUJITASK_POSTGRES_PORT:-5432}"
  postgres_db="${MUJITASK_POSTGRES_DB:-automation_business_scaffold}"
  postgres_user="${MUJITASK_POSTGRES_USER:-mujitask}"
  postgres_password="${MUJITASK_POSTGRES_PASSWORD:-mujitask}"
  postgres_admin_user="${MUJITASK_POSTGRES_ADMIN_USER:-$(id -un)}"
  postgres_socket_dir="${MUJITASK_POSTGRES_SOCKET_DIR:-/tmp}"

  local db_url runtime_migration_db_url fact_db_url fact_migration_db_url fact_runtime_role
  fact_runtime_role="$(require_config_value MUJITASK_FACT_RUNTIME_ROLE)"
  validate_pg_role_name "MUJITASK_FACT_RUNTIME_ROLE" "${fact_runtime_role}"
  if [[ "${MUJITASK_RUNTIME_MODE:-native}" == "external" ]]; then
    db_url="$(config_value MUJITASK_DB_URL BUSINESS_EXECUTION_CONTROL_DB_URL "")"
    runtime_migration_db_url="${configured_runtime_migration_db_url}"
    [[ -n "${runtime_migration_db_url}" ]] || fail_deploy "Missing MUJITASK_RUNTIME_MIGRATION_DB_URL in ${ENV_FILE}."
    fact_db_url="$(require_config_value MUJITASK_FACT_DB_URL BUSINESS_EXECUTION_CONTROL_FACT_DB_URL)"
    fact_migration_db_url="${configured_fact_migration_db_url}"
    [[ -n "${fact_migration_db_url}" ]] || fail_deploy "Missing MUJITASK_FACT_MIGRATION_DB_URL in ${ENV_FILE}."
  else
    local fact_runtime_password local_admin_db_url
    fact_runtime_password="$(require_config_value MUJITASK_FACT_RUNTIME_PASSWORD)"
    db_url="$(config_value MUJITASK_DB_URL BUSINESS_EXECUTION_CONTROL_DB_URL "$(compose_postgres_url "${postgres_user}" "${postgres_password}" "127.0.0.1" "${postgres_port}" "${postgres_db}")")"
    fact_db_url="$(config_value MUJITASK_FACT_DB_URL BUSINESS_EXECUTION_CONTROL_FACT_DB_URL "$(compose_postgres_url "${fact_runtime_role}" "${fact_runtime_password}" "127.0.0.1" "${postgres_port}" "${postgres_db}")")"
    local_admin_db_url="$(compose_local_admin_postgres_url "${postgres_admin_user}" "${postgres_socket_dir}" "${postgres_port}" "${postgres_db}")"
    runtime_migration_db_url="${db_url}"
    fact_migration_db_url="${configured_fact_migration_db_url:-${local_admin_db_url}}"
  fi
  if [[ -z "${db_url}" ]]; then
    fail_deploy "Missing database config. Set MUJITASK_DB_URL / BUSINESS_EXECUTION_CONTROL_DB_URL in ${ENV_FILE}."
  fi
  if [[ -z "${fact_db_url}" ]]; then
    fail_deploy "Missing Fact runtime database config. Set MUJITASK_FACT_DB_URL / BUSINESS_EXECUTION_CONTROL_FACT_DB_URL in ${ENV_FILE}."
  fi

  local artifact_root artifact_bucket artifact_store_provider
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

  local requested_by notification_channel_code tiktok_openclaw_agent_id amazon_openclaw_agent_id openclaw_state_dir amazon_feishu_account_id
  requested_by="$(config_value MUJITASK_REQUESTED_BY BUSINESS_EXECUTION_CONTROL_REQUESTED_BY "${agent_type}-skill")"
  notification_channel_code="$(config_value MUJITASK_NOTIFICATION_CHANNEL_CODE NOTIFICATION_CHANNEL_CODE "feishu_bot_api")"
  tiktok_openclaw_agent_id="$(config_value MUJITASK_TIKTOK_OPENCLAW_AGENT_ID MUJITASK_OPENCLAW_AGENT_ID "tiktok-ops")"
  amazon_openclaw_agent_id="$(config_value MUJITASK_AMAZON_OPENCLAW_AGENT_ID "" "amazon-ops")"
  amazon_feishu_account_id="$(require_config_value MUJITASK_AMAZON_FEISHU_ACCOUNT_ID)"
  openclaw_state_dir="$(config_value MUJITASK_OPENCLAW_STATE_DIR OPENCLAW_STATE_DIR "${HOME}/.openclaw")"

  prepare_project_tree "${install_dir}"
  ensure_project_install "${install_dir}"
  prepare_local_files "${install_dir}"
  start_runtime_services "${artifact_store_provider}"

  local executor_env_file="${install_dir}/scripts/execution_control/executor.local.env"
  validate_private_file_target "${executor_env_file}" "Executor environment file"
  local previous_umask
  previous_umask="$(umask)"
  umask 077
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
  umask "${previous_umask}"
  seal_private_file "${executor_env_file}" "Executor environment file"
  merge_key_value_file \
    "${executor_env_file}" \
    "MUJITASK_FEISHU_ACCESS_TOKEN=$(quote_env_value "${token}")" \
    "MUJITASK_FEISHU_BASE_URL=$(quote_env_value "${feishu_base_url}")" \
    "MUJITASK_FEISHU_AMAZON_PRODUCTS_ACCESS_TOKEN=$(quote_env_value "${amazon_products_access_token}")" \
    "BUSINESS_EXECUTION_CONTROL_FACT_DB_URL=$(quote_env_value "${fact_db_url}")" \
    "AMAZON_US_BROWSER_PROFILE_REF=$(quote_env_value "${amazon_us_browser_profile_ref}")"
  remove_key_value_file \
    "${executor_env_file}" \
    "TK_FACT_DB_URL" \
    "BUSINESS_EXECUTION_CONTROL_MIGRATION_ENV_FILE" \
    "BUSINESS_EXECUTION_CONTROL_MIGRATION_DB_URL" \
    "BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL" \
    "BUSINESS_EXECUTION_CONTROL_FACT_RUNTIME_ROLE" \
    "BUSINESS_EXECUTION_CONTROL_RUNTIME_MIGRATION_DB_URL" \
    "MUJITASK_RUNTIME_MIGRATION_DB_URL" \
    "MUJITASK_FACT_MIGRATION_DB_URL" \
    "MUJITASK_FACT_RUNTIME_ROLE" \
    "MUJITASK_FACT_RUNTIME_PASSWORD" \
    "MUJITASK_AMAZON_US_BROWSER_PROFILE_REF" \
    "MUJITASK_FEISHU_AMAZON_PRODUCTS_BASE_URL" \
    "MUJITASK_FEISHU_AMAZON_PRODUCTS_TABLE_ID" \
    "MUJITASK_FEISHU_AMAZON_PRODUCTS_VIEW_ID"
  seal_private_file "${executor_env_file}" "Executor environment file"

  local migration_env_file="${install_dir}/runtime/deployment/migration.local.env"
  write_fact_migration_env \
    "${migration_env_file}" \
    "${fact_migration_db_url}" \
    "${fact_runtime_role}"
  FACT_MIGRATION_ENV_FILE_TO_CLEAN="${migration_env_file}"

  wait_for_runtime \
    "${db_url}" \
    "${artifact_store_provider}" \
    "${minio_endpoint}" \
    "${minio_access_key}" \
    "${minio_secret_key}" \
    "${minio_secure}" \
    "${artifact_bucket}" \
    "${minio_create_bucket}"
  if [[ "${MUJITASK_RUNTIME_MODE:-native}" == "external" ]]; then
    verify_database_identity "Runtime" "${db_url}" "${runtime_migration_db_url}"
  fi
  verify_database_identity "Fact" "${fact_db_url}" "${fact_migration_db_url}"

  log "Running Runtime DB migrations"
  if [[ "${MUJITASK_RUNTIME_MODE:-native}" == "external" ]]; then
    BUSINESS_EXECUTION_CONTROL_RUNTIME_MIGRATION_DB_URL="${runtime_migration_db_url}" \
      bash "${install_dir}/scripts/execution_control/run_alembic_upgrade.sh"
  else
    bash "${install_dir}/scripts/execution_control/run_alembic_upgrade.sh"
  fi

  if [[ "${MUJITASK_RUNTIME_MODE:-native}" == "native" ]]; then
    bootstrap_native_legacy_schemas "${db_url}"
  fi

  log "Running Fact DB migrations"
  BUSINESS_EXECUTION_CONTROL_MIGRATION_ENV_FILE="${migration_env_file}" \
    bash "${install_dir}/scripts/execution_control/run_fact_alembic_upgrade.sh"
  if [[ "${MUJITASK_RUNTIME_MODE:-native}" == "native" ]]; then
    grant_native_fact_runtime_compatibility "${fact_migration_db_url}" "${fact_runtime_role}"
  fi
  verify_fact_runtime_privileges "${fact_db_url}" "${fact_runtime_role}"
  rm -f -- "${migration_env_file}"
  FACT_MIGRATION_ENV_FILE_TO_CLEAN=""

  local target_skill_dir
  INSTALLED_SKILL_DIR=""
  install_agent_skill \
    "${install_dir}" \
    "${tiktok_skills_dir}" \
    "${token}" \
    "${fastmoss_phone}" \
    "${fastmoss_password}" \
    "${notification_channel_code}" \
    "${tiktok_openclaw_agent_id}" \
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

  local amazon_target_skill_dir
  INSTALLED_AMAZON_SKILL_DIR=""
  install_amazon_agent_skill \
    "${install_dir}" \
    "${amazon_skills_dir}" \
    "${notification_channel_code}" \
    "${amazon_openclaw_agent_id}" \
    "${openclaw_state_dir}" \
    "${amazon_feishu_account_id}" \
    "${amazon_products_base_url}" \
    "${amazon_products_table_id}" \
    "${amazon_products_view_id}"
  amazon_target_skill_dir="${INSTALLED_AMAZON_SKILL_DIR}"
  [[ -n "${amazon_target_skill_dir}" ]] || fail_deploy "Amazon skill installation did not return a target directory."

  local resolved_ref="local-checkout"
  if command -v git >/dev/null 2>&1 && git -C "${SOURCE_DIR}" rev-parse --short HEAD >/dev/null 2>&1; then
    resolved_ref="$(git -C "${SOURCE_DIR}" rev-parse --short HEAD)"
  fi
  write_deploy_state "${install_dir}" "${MUJITASK_REPO_URL:-local-checkout}" "${resolved_ref}" "" "${LAST_FRAMEWORK_ARCHIVE_URL:-}"

  if [[ "${MUJITASK_RUNTIME_MODE:-native}" == "external" ]]; then
    log "Installing launchd agents without Runtime/TikTok schema bootstrap"
    install_external_launch_agents "${install_dir}"
  else
    log "Installing launchd agents"
    bash "${install_dir}/scripts/execution_control/install_launch_agents.sh"
  fi

  smoke_check "${install_dir}" "${target_skill_dir}" "${install_dir}/scripts/execution_control/executor.local.env"

  log "Deployment completed."
  log "Project directory: ${install_dir}"
  log "Agent type: ${agent_type}"
  log "TikTok skill directory: ${target_skill_dir}"
  log "Amazon skill directory: ${amazon_target_skill_dir}"
  log "Runtime mode: ${MUJITASK_RUNTIME_MODE:-native}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
