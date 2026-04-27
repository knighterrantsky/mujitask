#!/usr/bin/env bash

set -euo pipefail

OPENCLAW_LOG_PREFIX="update-openclaw"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./openclaw_deploy_common.sh
source "$SCRIPT_DIR/openclaw_deploy_common.sh"

main() {
  ensure_uv
  ensure_python_311

  local openclaw_skills_dir
  openclaw_skills_dir="$(resolve_openclaw_skills_dir)"
  local target_skill_dir="$openclaw_skills_dir/mujitask-tiktok-feishu-sync"
  local existing_skill_env="$target_skill_dir/skill.local.env"
  [[ -f "$existing_skill_env" ]] || fail "Missing deployed skill config: $existing_skill_env"

  local install_dir token browser_profile_ref fastmoss_phone fastmoss_password
  local feishu_base_url
  local tk_selection_table_id tk_selection_view_id
  local tk_competitor_table_id tk_competitor_view_id
  local tk_influencer_pool_table_id tk_influencer_pool_view_id
  local tk_influencer_outreach_table_id tk_influencer_outreach_view_id
  local tk_hot_video_table_id tk_hot_video_view_id
  local db_url artifact_root artifact_bucket requested_by notification_channel_code
  local openclaw_agent_id openclaw_state_dir
  install_dir="$(read_kv_value "$existing_skill_env" "INSTALL_DIR" 2>/dev/null || true)"
  feishu_base_url="$(read_kv_value "$existing_skill_env" "MUJITASK_FEISHU_BASE_URL" 2>/dev/null || true)"
  tk_selection_table_id="$(read_kv_value "$existing_skill_env" "MUJITASK_FEISHU_TK_SELECTION_TABLE_ID" 2>/dev/null || true)"
  tk_selection_view_id="$(read_kv_value "$existing_skill_env" "MUJITASK_FEISHU_TK_SELECTION_VIEW_ID" 2>/dev/null || true)"
  tk_competitor_table_id="$(read_kv_value "$existing_skill_env" "MUJITASK_FEISHU_TK_COMPETITOR_TABLE_ID" 2>/dev/null || true)"
  tk_competitor_view_id="$(read_kv_value "$existing_skill_env" "MUJITASK_FEISHU_TK_COMPETITOR_VIEW_ID" 2>/dev/null || true)"
  tk_influencer_pool_table_id="$(read_kv_value "$existing_skill_env" "MUJITASK_FEISHU_TK_INFLUENCER_POOL_TABLE_ID" 2>/dev/null || true)"
  tk_influencer_pool_view_id="$(read_kv_value "$existing_skill_env" "MUJITASK_FEISHU_TK_INFLUENCER_POOL_VIEW_ID" 2>/dev/null || true)"
  tk_influencer_outreach_table_id="$(read_kv_value "$existing_skill_env" "MUJITASK_FEISHU_TK_INFLUENCER_OUTREACH_TABLE_ID" 2>/dev/null || true)"
  tk_influencer_outreach_view_id="$(read_kv_value "$existing_skill_env" "MUJITASK_FEISHU_TK_INFLUENCER_OUTREACH_VIEW_ID" 2>/dev/null || true)"
  tk_hot_video_table_id="$(read_kv_value "$existing_skill_env" "MUJITASK_FEISHU_TK_HOT_VIDEO_TABLE_ID" 2>/dev/null || true)"
  tk_hot_video_view_id="$(read_kv_value "$existing_skill_env" "MUJITASK_FEISHU_TK_HOT_VIDEO_VIEW_ID" 2>/dev/null || true)"
  token="$(read_kv_value "$existing_skill_env" "MUJITASK_FEISHU_ACCESS_TOKEN" 2>/dev/null || true)"
  browser_profile_ref="$(read_kv_value "$existing_skill_env" "BROWSER_PROFILE_REF" 2>/dev/null || true)"
  fastmoss_phone="$(read_kv_value "$existing_skill_env" "FASTMOSS_PHONE" 2>/dev/null || true)"
  fastmoss_password="$(read_kv_value "$existing_skill_env" "FASTMOSS_PASSWORD" 2>/dev/null || true)"
  db_url="$(read_kv_value "$existing_skill_env" "EXECUTION_CONTROL_DB_URL" 2>/dev/null || true)"
  artifact_root="$(read_kv_value "$existing_skill_env" "EXECUTION_CONTROL_ARTIFACT_ROOT" 2>/dev/null || true)"
  artifact_bucket="$(read_kv_value "$existing_skill_env" "EXECUTION_CONTROL_ARTIFACT_BUCKET" 2>/dev/null || true)"
  requested_by="$(read_kv_value "$existing_skill_env" "EXECUTION_CONTROL_REQUESTED_BY" 2>/dev/null || true)"
  notification_channel_code="$(read_kv_value "$existing_skill_env" "NOTIFICATION_CHANNEL_CODE" 2>/dev/null || true)"
  openclaw_agent_id="$(read_kv_value "$existing_skill_env" "OPENCLAW_AGENT_ID" 2>/dev/null || true)"
  openclaw_state_dir="$(read_kv_value "$existing_skill_env" "OPENCLAW_STATE_DIR" 2>/dev/null || true)"
  [[ -n "$install_dir" ]] || fail "INSTALL_DIR is missing in $existing_skill_env."
  [[ -n "$feishu_base_url" ]] || fail "MUJITASK_FEISHU_BASE_URL is missing in $existing_skill_env."
  [[ -n "$tk_selection_table_id" ]] || fail "MUJITASK_FEISHU_TK_SELECTION_TABLE_ID is missing in $existing_skill_env."
  [[ -n "$tk_selection_view_id" ]] || fail "MUJITASK_FEISHU_TK_SELECTION_VIEW_ID is missing in $existing_skill_env."
  [[ -n "$tk_competitor_table_id" ]] || fail "MUJITASK_FEISHU_TK_COMPETITOR_TABLE_ID is missing in $existing_skill_env."
  [[ -n "$tk_competitor_view_id" ]] || fail "MUJITASK_FEISHU_TK_COMPETITOR_VIEW_ID is missing in $existing_skill_env."
  [[ -n "$tk_influencer_pool_table_id" ]] || fail "MUJITASK_FEISHU_TK_INFLUENCER_POOL_TABLE_ID is missing in $existing_skill_env."
  [[ -n "$tk_influencer_pool_view_id" ]] || fail "MUJITASK_FEISHU_TK_INFLUENCER_POOL_VIEW_ID is missing in $existing_skill_env."
  [[ -n "$tk_influencer_outreach_table_id" ]] || fail "MUJITASK_FEISHU_TK_INFLUENCER_OUTREACH_TABLE_ID is missing in $existing_skill_env."
  [[ -n "$tk_influencer_outreach_view_id" ]] || fail "MUJITASK_FEISHU_TK_INFLUENCER_OUTREACH_VIEW_ID is missing in $existing_skill_env."
  [[ -n "$tk_hot_video_table_id" ]] || fail "MUJITASK_FEISHU_TK_HOT_VIDEO_TABLE_ID is missing in $existing_skill_env."
  [[ -n "$tk_hot_video_view_id" ]] || fail "MUJITASK_FEISHU_TK_HOT_VIDEO_VIEW_ID is missing in $existing_skill_env."
  [[ -n "$token" ]] || fail "MUJITASK_FEISHU_ACCESS_TOKEN is missing in $existing_skill_env."

  local existing_executor_env="$install_dir/scripts/execution_control/executor.local.env"
  local artifact_store_provider="" artifact_object_prefix="" minio_endpoint="" minio_access_key="" minio_secret_key="" minio_region="" minio_secure="" minio_create_bucket="" sync_referenced_files=""
  if [[ -f "$existing_executor_env" ]]; then
    [[ -n "$db_url" ]] || db_url="$(read_kv_value "$existing_executor_env" "BUSINESS_EXECUTION_CONTROL_DB_URL" 2>/dev/null || true)"
    [[ -n "$artifact_root" ]] || artifact_root="$(read_kv_value "$existing_executor_env" "BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT" 2>/dev/null || true)"
    [[ -n "$artifact_bucket" ]] || artifact_bucket="$(read_kv_value "$existing_executor_env" "BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET" 2>/dev/null || true)"
    artifact_store_provider="$(read_kv_value "$existing_executor_env" "BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER" 2>/dev/null || true)"
    artifact_object_prefix="$(read_kv_value "$existing_executor_env" "BUSINESS_EXECUTION_CONTROL_ARTIFACT_OBJECT_PREFIX" 2>/dev/null || true)"
    minio_endpoint="$(read_kv_value "$existing_executor_env" "BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT" 2>/dev/null || true)"
    minio_access_key="$(read_kv_value "$existing_executor_env" "BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY" 2>/dev/null || true)"
    minio_secret_key="$(read_kv_value "$existing_executor_env" "BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY" 2>/dev/null || true)"
    minio_region="$(read_kv_value "$existing_executor_env" "BUSINESS_EXECUTION_CONTROL_MINIO_REGION" 2>/dev/null || true)"
    minio_secure="$(read_kv_value "$existing_executor_env" "BUSINESS_EXECUTION_CONTROL_MINIO_SECURE" 2>/dev/null || true)"
    minio_create_bucket="$(read_kv_value "$existing_executor_env" "BUSINESS_EXECUTION_CONTROL_MINIO_CREATE_BUCKET" 2>/dev/null || true)"
    sync_referenced_files="$(read_kv_value "$existing_executor_env" "BUSINESS_EXECUTION_CONTROL_SYNC_REFERENCED_FILES" 2>/dev/null || true)"
    [[ -n "$requested_by" ]] || requested_by="$(read_kv_value "$existing_executor_env" "BUSINESS_EXECUTION_CONTROL_REQUESTED_BY" 2>/dev/null || true)"
    [[ -n "$notification_channel_code" ]] || notification_channel_code="$(read_kv_value "$existing_executor_env" "NOTIFICATION_CHANNEL_CODE" 2>/dev/null || true)"
  fi

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
  write_skill_local_env \
    "$target_skill_dir" \
    "$install_dir" \
    "$feishu_base_url" \
    "$tk_selection_table_id" \
    "$tk_selection_view_id" \
    "$tk_competitor_table_id" \
    "$tk_competitor_view_id" \
    "$tk_influencer_pool_table_id" \
    "$tk_influencer_pool_view_id" \
    "$tk_influencer_outreach_table_id" \
    "$tk_influencer_outreach_view_id" \
    "$tk_hot_video_table_id" \
    "$tk_hot_video_view_id" \
    "$token" \
    "$browser_profile_ref" \
    "$fastmoss_phone" \
    "$fastmoss_password" \
    "$db_url" \
    "$artifact_root" \
    "$artifact_bucket" \
    "${requested_by:-openclaw-skill}" \
    "${notification_channel_code:-feishu_bot_api}" \
    "${openclaw_agent_id:-tiktok-ops}" \
    "${openclaw_state_dir:-$HOME/.openclaw}"
  write_executor_local_env \
    "$install_dir" \
    "$db_url" \
    "$artifact_root" \
    "$artifact_bucket" \
    "${artifact_store_provider:-minio}" \
    "${artifact_object_prefix:-mujitask/local}" \
    "$minio_endpoint" \
    "$minio_access_key" \
    "$minio_secret_key" \
    "$minio_region" \
    "${minio_secure:-false}" \
    "${minio_create_bucket:-true}" \
    "${sync_referenced_files:-true}" \
    "${requested_by:-openclaw-skill}" \
    "$token" \
    "$browser_profile_ref" \
    "$fastmoss_phone" \
    "$fastmoss_password" \
    "${notification_channel_code:-feishu_bot_api}"
  write_deploy_state "$install_dir" "$repo_url" "$resolved_ref" "${archive_url:-}" "${LAST_FRAMEWORK_ARCHIVE_URL:-}"
  log "Refreshing launchd agents"
  bash "$install_dir/scripts/execution_control/install_launch_agents.sh"

  smoke_check "$install_dir" "$target_skill_dir" "$install_dir/scripts/execution_control/executor.local.env"

  log "Update completed."
  log "Installed ref: $resolved_ref"
  log "Project directory: $install_dir"
  log "OpenClaw skill directory: $target_skill_dir"
  log "Chrome binary: $chrome_bin"
}

main "$@"
