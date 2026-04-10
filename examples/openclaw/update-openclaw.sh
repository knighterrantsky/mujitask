#!/usr/bin/env bash

set -euo pipefail

OPENCLAW_LOG_PREFIX="update-openclaw"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./openclaw_deploy_common.sh
source "$SCRIPT_DIR/openclaw_deploy_common.sh"

main() {
  ensure_uv
  ensure_python_311

  local openclaw_skills_dir="$HOME/.openclaw/workspace/skills"
  local target_skill_dir="$openclaw_skills_dir/mujitask-tiktok-feishu-sync"
  local existing_skill_env="$target_skill_dir/skill.local.env"
  [[ -f "$existing_skill_env" ]] || fail "Missing deployed skill config: $existing_skill_env"

  local install_dir table_url token
  install_dir="$(read_kv_value "$existing_skill_env" "INSTALL_DIR" 2>/dev/null || true)"
  table_url="$(read_kv_value "$existing_skill_env" "TABLE_URL" 2>/dev/null || true)"
  token="$(read_kv_value "$existing_skill_env" "FEISHU_ACCESS_TOKEN" 2>/dev/null || true)"
  [[ -n "$install_dir" ]] || fail "INSTALL_DIR is missing in $existing_skill_env."
  [[ -n "$table_url" ]] || fail "TABLE_URL is missing in $existing_skill_env."
  [[ -n "$token" ]] || fail "FEISHU_ACCESS_TOKEN is missing in $existing_skill_env."

  local deploy_state_path="$install_dir/runtime/deployment/openclaw-deploy.env"
  [[ -f "$deploy_state_path" ]] || fail "Missing deployment state: $deploy_state_path"
  deploy_state_supports_update "$deploy_state_path" \
    || fail "This install does not support update-openclaw.sh yet. Reinstall with the new deploy-openclaw.sh first."

  local repo_url existing_repo_archive_url existing_last_ref
  repo_url="$(read_kv_value "$deploy_state_path" "REPO_URL" 2>/dev/null || true)"
  existing_repo_archive_url="$(read_kv_value "$deploy_state_path" "REPO_ARCHIVE_URL" 2>/dev/null || true)"
  existing_last_ref="$(read_kv_value "$deploy_state_path" "LAST_RESOLVED_REF" 2>/dev/null || true)"
  [[ -n "$repo_url" ]] || fail "REPO_URL is missing in $deploy_state_path."

  if [[ -n "$existing_last_ref" ]]; then
    log "Current installed ref: $existing_last_ref"
  fi

  local github_token_input=""
  if [[ -z "$GITHUB_AUTH_TOKEN" ]]; then
    github_token_input="$(prompt_secret_optional "GitHub PAT for private GitHub repos (optional, press Enter to skip)")"
    if [[ -n "$github_token_input" ]]; then
      GITHUB_AUTH_TOKEN="$github_token_input"
    fi
  fi

  local tag resolved_ref archive_url github_slug repo_archive
  tag="$(prompt_optional "Tag (leave blank to auto-resolve latest)" "")"
  repo_archive="$TMP_ROOT/project-archive"

  if github_slug="$(parse_github_slug "$repo_url" 2>/dev/null)"; then
    resolved_ref="$tag"
    if [[ -z "$resolved_ref" ]]; then
      log "Resolving latest release/tag for $github_slug"
      resolved_ref="$(resolve_latest_github_ref "$github_slug")"
    fi
    archive_url="https://api.github.com/repos/$github_slug/zipball/$resolved_ref"
    repo_archive="$repo_archive.zip"
  else
    archive_url="$existing_repo_archive_url"
    [[ -n "$archive_url" ]] || fail "REPO_ARCHIVE_URL is missing in $deploy_state_path for non-GitHub repo."
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
  local project_root
  project_root="$(extract_archive "$repo_archive" "$extracted_project_dir")"

  log "Updating project directory in place"
  sync_install_tree "$project_root" "$install_dir"

  local pyproject_path="$install_dir/pyproject.toml"
  [[ -f "$pyproject_path" ]] || fail "Missing $pyproject_path after update sync."

  local venv_python="$install_dir/.venv/bin/python"
  [[ -x "$venv_python" ]] || fail "Existing virtual environment is missing at $venv_python. Reinstall with deploy-openclaw.sh."

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
    warn "Install Chrome and rerun this update script."
    exit 2
  fi

  local source_skill_dir="$install_dir/skills/mujitask-tiktok-feishu-sync"
  [[ -d "$source_skill_dir" ]] || fail "Missing skill bundle at $source_skill_dir."

  local previous_skill_env="$TMP_ROOT/previous-skill.local.env"
  cp "$existing_skill_env" "$previous_skill_env"

  replace_target_dir "$target_skill_dir"
  cp -R "$source_skill_dir"/. "$target_skill_dir"/
  cp "$previous_skill_env" "$target_skill_dir/skill.local.env"
  write_skill_local_env "$target_skill_dir" "$install_dir" "$table_url" "$token"
  write_deploy_state "$install_dir" "$repo_url" "$resolved_ref" "${archive_url:-}" "${LAST_FRAMEWORK_ARCHIVE_URL:-}"

  smoke_check "$install_dir" "$target_skill_dir"

  log "Update completed."
  log "Installed ref: $resolved_ref"
  log "Project directory: $install_dir"
  log "OpenClaw skill directory: $target_skill_dir"
  log "Chrome binary: $chrome_bin"
}

main "$@"
