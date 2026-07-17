#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ENV_FILE="${MUJITASK_DEPLOY_ENV_FILE:-${SOURCE_DIR}/scripts/deploy/macos/deploy.local.env}"

STATUS=0

log() {
  printf '[mujitask-preflight] %s\n' "$*"
}

warn() {
  printf '[mujitask-preflight] WARN: %s\n' "$*" >&2
}

error() {
  printf '[mujitask-preflight] ERROR: %s\n' "$*" >&2
  STATUS=1
}

load_deploy_env_if_present() {
  [[ -f "${ENV_FILE}" ]] || return 0
  if [[ -L "${ENV_FILE}" ]]; then
    error "Deployment environment file must not be a symlink: ${ENV_FILE}."
    return 1
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
    error "Deployment environment file must have mode 400 or 600: ${ENV_FILE}."
    return 1
  fi
  if [[ "${env_owner}" != "$(id -u)" ]]; then
    error "Deployment environment file must be owned by the current user: ${ENV_FILE}."
    return 1
  fi

  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
}

if ! load_deploy_env_if_present; then
  exit "${STATUS}"
fi

require_command() {
  command -v "$1" >/dev/null 2>&1 || error "$1 is required."
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

require_config_value() {
  local primary="$1"
  local fallback="${2:-}"
  local value
  value="$(config_value "${primary}" "${fallback}" "")"
  [[ -n "${value}" ]] || error "Missing ${primary}${fallback:+ / ${fallback}}. Copy scripts/deploy/macos/deploy.local.env.example to ${ENV_FILE} and fill it in."
}

validate_pg_role_name() {
  local config_key="$1"
  local role_name="$2"
  if [[ ! "${role_name}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ || ${#role_name} -gt 63 ]]; then
    error "Invalid ${config_key}=${role_name}. Use an unquoted Postgres identifier of at most 63 characters."
  fi
}

require_feishu_table_config() {
  require_feishu_table_route "TK_SELECTION"
  require_feishu_table_route "TK_COMPETITOR"
  require_feishu_table_route "TK_INFLUENCER_POOL"
  require_feishu_table_route "TK_INFLUENCER_OUTREACH"
  require_feishu_table_route "TK_HOT_VIDEO"
  require_feishu_table_route "AMAZON_PRODUCTS"
}

require_feishu_table_route() {
  local env_slug="$1"
  local base_url table_id view_id
  if [[ "${env_slug}" == "AMAZON_PRODUCTS" ]]; then
    base_url="$(config_value MUJITASK_FEISHU_AMAZON_PRODUCTS_BASE_URL MUJITASK_FEISHU_BASE_URL "")"
  else
    base_url="$(config_value MUJITASK_FEISHU_BASE_URL "" "")"
  fi
  table_id="$(config_value "MUJITASK_FEISHU_${env_slug}_TABLE_ID" "" "")"
  view_id="$(config_value "MUJITASK_FEISHU_${env_slug}_VIEW_ID" "" "")"
  if [[ -z "${base_url}" || -z "${table_id}" || -z "${view_id}" ]]; then
    error "Missing Feishu table route for ${env_slug}. Configure MUJITASK_FEISHU_BASE_URL plus MUJITASK_FEISHU_${env_slug}_TABLE_ID and MUJITASK_FEISHU_${env_slug}_VIEW_ID."
  fi
}

check_port_hint() {
  local port="$1"
  local label="$2"
  if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
    warn "${label} port ${port} is already listening. This is fine if it belongs to this deployment; otherwise adjust the port in ${ENV_FILE}."
  fi
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  error "macOS is required for this one-click deployment path."
fi

require_command curl
require_command launchctl

if command -v uv >/dev/null 2>&1; then
  log "OK: uv is installed."
else
  log "uv is not installed yet; deploy.sh will install it with the official uv installer."
fi

if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  log "OK: Node.js and npm are installed."
else
  log "Node.js/npm are not installed yet; deploy.sh will install node with Homebrew."
fi

RUNTIME_MODE="${MUJITASK_RUNTIME_MODE:-native}"
if [[ "${RUNTIME_MODE}" == "native" ]]; then
  require_command brew
  if command -v psql >/dev/null 2>&1; then
    log "OK: psql is installed."
  else
    log "psql is not installed yet; deploy.sh will install ${MUJITASK_POSTGRES_FORMULA:-postgresql@17} with Homebrew."
  fi
  if command -v minio >/dev/null 2>&1; then
    log "OK: minio is installed."
  else
    log "minio is not installed yet; deploy.sh will install it with Homebrew."
  fi
  check_port_hint "${MUJITASK_POSTGRES_PORT:-5432}" "Postgres"
  check_port_hint "${MUJITASK_MINIO_PORT:-9000}" "MinIO API"
  check_port_hint "${MUJITASK_MINIO_CONSOLE_PORT:-9001}" "MinIO console"
  if [[ "${MUJITASK_PREFLIGHT_REQUIRE_ENV:-0}" == "1" ]]; then
    require_config_value MUJITASK_FACT_RUNTIME_PASSWORD
    validate_pg_role_name "MUJITASK_POSTGRES_USER" "${MUJITASK_POSTGRES_USER:-mujitask}"
    if [[ "${MUJITASK_FACT_RUNTIME_ROLE:-}" == "${MUJITASK_POSTGRES_USER:-mujitask}" ]]; then
      error "MUJITASK_FACT_RUNTIME_ROLE must differ from MUJITASK_POSTGRES_USER in native mode."
    fi
    if [[ "${MUJITASK_POSTGRES_ADMIN_USER:-$(id -un)}" == "${MUJITASK_FACT_RUNTIME_ROLE:-}" ]]; then
      error "MUJITASK_POSTGRES_ADMIN_USER must differ from MUJITASK_FACT_RUNTIME_ROLE in native mode."
    fi
  fi
elif [[ "${RUNTIME_MODE}" == "external" ]]; then
  log "Runtime mode is external; deploy.sh will not start local Postgres/MinIO."
  EXTERNAL_DB_URL="$(config_value MUJITASK_DB_URL BUSINESS_EXECUTION_CONTROL_DB_URL "")"
  if [[ -z "${EXTERNAL_DB_URL}" ]]; then
    error "MUJITASK_RUNTIME_MODE=external requires MUJITASK_DB_URL / BUSINESS_EXECUTION_CONTROL_DB_URL."
  fi
  require_config_value MUJITASK_RUNTIME_MIGRATION_DB_URL
  require_config_value MUJITASK_FACT_DB_URL BUSINESS_EXECUTION_CONTROL_FACT_DB_URL
  require_config_value MUJITASK_FACT_MIGRATION_DB_URL
  EXTERNAL_ARTIFACT_PROVIDER="$(config_value MUJITASK_ARTIFACT_STORE_PROVIDER BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER "minio")"
  if [[ "${EXTERNAL_ARTIFACT_PROVIDER}" == "minio" ]]; then
    require_config_value MUJITASK_MINIO_ENDPOINT BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT
    require_config_value MUJITASK_MINIO_ROOT_USER BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY
    require_config_value MUJITASK_MINIO_ROOT_PASSWORD BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY
  fi
else
  error "Unsupported MUJITASK_RUNTIME_MODE=${RUNTIME_MODE}. Use native or external."
fi

if [[ "${MUJITASK_PREFLIGHT_REQUIRE_ENV:-0}" == "1" ]]; then
  require_config_value MUJITASK_INSTALL_DIR INSTALL_DIR
  require_config_value MUJITASK_TIKTOK_SKILLS_DIR MUJITASK_SKILLS_DIR
  require_config_value MUJITASK_AMAZON_SKILLS_DIR
  require_config_value MUJITASK_TIKTOK_OPENCLAW_AGENT_ID MUJITASK_OPENCLAW_AGENT_ID
  require_config_value MUJITASK_AMAZON_OPENCLAW_AGENT_ID
  require_config_value MUJITASK_AMAZON_FEISHU_ACCOUNT_ID
  require_feishu_table_config
  require_config_value MUJITASK_FEISHU_ACCESS_TOKEN
  require_config_value MUJITASK_FASTMOSS_PHONE FASTMOSS_PHONE
  require_config_value MUJITASK_FASTMOSS_PASSWORD FASTMOSS_PASSWORD
  require_config_value MUJITASK_AMAZON_US_BROWSER_PROFILE_REF
  require_config_value MUJITASK_FACT_RUNTIME_ROLE
  validate_pg_role_name "MUJITASK_FACT_RUNTIME_ROLE" "${MUJITASK_FACT_RUNTIME_ROLE:-}"
fi

if [[ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" || -x "${HOME}/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]]; then
  log "OK: Google Chrome is installed."
else
  warn "Google Chrome was not found. This is acceptable only if the configured browser profile uses Roxy or another remote provider."
fi

INSTALL_TARGET="$(config_value MUJITASK_INSTALL_DIR INSTALL_DIR "${HOME}/apps/mujitask")"
TIKTOK_SKILLS_TARGET="$(config_value MUJITASK_TIKTOK_SKILLS_DIR MUJITASK_SKILLS_DIR "")"
AMAZON_SKILLS_TARGET="$(config_value MUJITASK_AMAZON_SKILLS_DIR "" "")"
AGENT_TYPE="$(config_value MUJITASK_AGENT_TYPE AGENT_TYPE "generic")"
log "Project install target: ${INSTALL_TARGET}"
log "Agent type: ${AGENT_TYPE}"
if [[ -n "${TIKTOK_SKILLS_TARGET}" ]]; then
  log "TikTok skills install target: ${TIKTOK_SKILLS_TARGET}"
else
  warn "TikTok skills install target is not set yet."
fi
if [[ -n "${AMAZON_SKILLS_TARGET}" ]]; then
  log "Amazon skills install target: ${AMAZON_SKILLS_TARGET}"
else
  warn "Amazon skills install target is not set yet."
fi

if [[ "${STATUS}" -eq 0 ]]; then
  log "Preflight passed."
else
  warn "Preflight failed. Fix the errors above and rerun."
fi

exit "${STATUS}"
