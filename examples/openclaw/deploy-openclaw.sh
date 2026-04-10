#!/usr/bin/env bash

set -euo pipefail

OPENCLAW_LOG_PREFIX="deploy-openclaw"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./openclaw_deploy_common.sh
source "$SCRIPT_DIR/openclaw_deploy_common.sh"

main() {
  ensure_uv
  ensure_python_311

  local openclaw_skills_dir="$HOME/.openclaw/workspace/skills"
  local existing_skill_env="$openclaw_skills_dir/mujitask-tiktok-feishu-sync/skill.local.env"
  local existing_install_dir=""
  existing_install_dir="$(read_kv_value "$existing_skill_env" "INSTALL_DIR" 2>/dev/null || true)"

  local repo_url="" tag="" install_dir="" table_url="" token="" archive_url="" github_slug="" resolved_ref="" github_token_input=""
  local default_install_dir="$HOME/apps/mujitask"
  if [[ -n "$existing_install_dir" ]]; then
    default_install_dir="$existing_install_dir"
  fi

  if [[ -z "$GITHUB_AUTH_TOKEN" ]]; then
    github_token_input="$(prompt_secret_optional "GitHub PAT for private GitHub repos (optional, press Enter to skip)")"
    if [[ -n "$github_token_input" ]]; then
      GITHUB_AUTH_TOKEN="$github_token_input"
    fi
  fi

  tag="$(prompt_optional "Tag (leave blank to auto-resolve latest)" "")"
  install_dir="$(prompt "Install directory" "$default_install_dir")"

  local deploy_state_path="$install_dir/runtime/deployment/openclaw-deploy.env"
  if [[ -f "$deploy_state_path" ]] && deploy_state_supports_update "$deploy_state_path"; then
    fail "Managed install already exists at $install_dir. Use update-openclaw.sh instead."
  fi
  if [[ -e "$install_dir" ]] && directory_has_entries "$install_dir"; then
    fail "Install directory already exists and is not empty: $install_dir. Choose a new directory or clear it manually."
  fi
  mkdir -p "$install_dir"

  if [[ -f "$existing_skill_env" ]]; then
    table_url="$(read_kv_value "$existing_skill_env" "TABLE_URL" 2>/dev/null || true)"
    token="$(read_kv_value "$existing_skill_env" "FEISHU_ACCESS_TOKEN" 2>/dev/null || true)"
  fi

  repo_url="$(prompt "Repo URL")"

  if [[ -n "$table_url" ]]; then
    log "Reusing existing Feishu table URL from $existing_skill_env"
  else
    table_url="$(prompt "Feishu table URL")"
  fi

  if [[ -n "$token" ]]; then
    log "Reusing existing Feishu access token from $existing_skill_env"
  else
    token="$(prompt_secret "Feishu access token")"
  fi

  local repo_archive="$TMP_ROOT/project-archive"
  local project_root

  if github_slug="$(parse_github_slug "$repo_url" 2>/dev/null)"; then
    resolved_ref="$tag"
    if [[ -z "$resolved_ref" ]]; then
      log "Resolving latest release/tag for $github_slug"
      resolved_ref="$(resolve_latest_github_ref "$github_slug")"
    fi
    archive_url="https://api.github.com/repos/$github_slug/zipball/$resolved_ref"
    repo_archive="$repo_archive.zip"
  else
    archive_url="$(prompt "Archive URL for the repository source package")"
    local lowered_archive
    lowered_archive="$(to_lower "$archive_url")"
    if [[ "$lowered_archive" == *.tar.gz || "$lowered_archive" == *.tgz || "$lowered_archive" == *.tar ]]; then
      repo_archive="$repo_archive.tar.gz"
    else
      repo_archive="$repo_archive.zip"
    fi
    resolved_ref="${tag:-custom-archive}"
  fi

  log "Downloading project archive"
  download_file "$archive_url" "$repo_archive"

  local extracted_project_dir="$TMP_ROOT/project-extracted"
  project_root="$(extract_archive "$repo_archive" "$extracted_project_dir")"

  cp -R "$project_root"/. "$install_dir"/

  local pyproject_path="$install_dir/pyproject.toml"
  [[ -f "$pyproject_path" ]] || fail "Missing $pyproject_path after extraction."

  log "Creating project virtual environment"
  "$UV_BIN" venv --python 3.11 "$install_dir/.venv"

  local venv_python="$install_dir/.venv/bin/python"
  [[ -x "$venv_python" ]] || fail "Virtual environment python was not created."

  install_framework_from_pyproject "$pyproject_path" "$venv_python"

  local project_deps=()
  local dep
  while IFS= read -r dep; do
    [[ -n "$dep" ]] && project_deps+=("$dep")
  done < <(read_project_dependencies "$install_dir/pyproject.toml")
  if ((${#project_deps[@]} > 0)); then
    log "Installing project runtime dependencies"
    "$UV_BIN" pip install --python "$venv_python" "${project_deps[@]}"
  fi

  log "Installing project package"
  "$UV_BIN" pip install --python "$venv_python" -e "$install_dir" --no-deps

  log "Installing Playwright Chromium"
  "$venv_python" -m playwright install chromium

  mkdir -p "$install_dir/runtime/cli_runs" "$install_dir/runtime/artifacts" "$install_dir/runtime/downloads"
  write_browser_profiles_if_missing "$install_dir"

  local chrome_bin
  chrome_bin="$(detect_chrome_bin || true)"
  if [[ -z "$chrome_bin" ]]; then
    warn "Google Chrome was not found."
    warn "Install Chrome and rerun this deployment script."
    exit 2
  fi

  local source_skill_dir="$install_dir/skills/mujitask-tiktok-feishu-sync"
  local target_skill_dir="$openclaw_skills_dir/mujitask-tiktok-feishu-sync"
  local previous_skill_env="$TMP_ROOT/previous-skill.local.env"

  [[ -d "$source_skill_dir" ]] || fail "Missing skill bundle at $source_skill_dir."
  if [[ -f "$existing_skill_env" ]]; then
    cp "$existing_skill_env" "$previous_skill_env"
  fi

  replace_target_dir "$target_skill_dir"
  cp -R "$source_skill_dir"/. "$target_skill_dir"/
  if [[ -f "$previous_skill_env" ]]; then
    cp "$previous_skill_env" "$target_skill_dir/skill.local.env"
  fi
  write_skill_local_env "$target_skill_dir" "$install_dir" "$table_url" "$token"
  write_deploy_state "$install_dir" "$repo_url" "$resolved_ref" "${archive_url:-}" "${LAST_FRAMEWORK_ARCHIVE_URL:-}"

  smoke_check "$install_dir" "$target_skill_dir"

  log "Deployment completed."
  log "Installed ref: $resolved_ref"
  log "Project directory: $install_dir"
  log "OpenClaw skill directory: $target_skill_dir"
  log "Chrome binary: $chrome_bin"
}

main "$@"
