#!/usr/bin/env bash
set -euo pipefail

readonly TARGET_UID="$(id -u)"
readonly DEBUG_SWITCH="--remote-debugging-port=9222"
readonly PROFILE_SWITCH="--user-data-dir=${HOME}/.mujitask/chrome-cdp/chrome-gcp"
readonly GRACE_SECONDS=10

target_pids=()
while read -r process_uid process_pid process_command; do
  [[ "${process_uid}" == "${TARGET_UID}" ]] || continue
  [[ "${process_pid}" =~ ^[0-9]+$ ]] || continue
  [[ "${process_command}" == *"${DEBUG_SWITCH}"* ]] || continue
  [[ "${process_command}" == *"${PROFILE_SWITCH}"* ]] || continue
  if [[ "${process_command}" != *"Google Chrome"* && "${process_command}" != *"Chromium"* ]]; then
    continue
  fi
  target_pids+=("${process_pid}")
done < <(/bin/ps -axo uid=,pid=,command=)

if ((${#target_pids[@]} == 0)); then
  exit 0
fi

/bin/kill -TERM "${target_pids[@]}" 2>/dev/null || true
deadline=$((SECONDS + GRACE_SECONDS))

while ((SECONDS < deadline)); do
  remaining=()
  for process_pid in "${target_pids[@]}"; do
    if /bin/kill -0 "${process_pid}" 2>/dev/null; then
      remaining+=("${process_pid}")
    fi
  done
  if ((${#remaining[@]} == 0)); then
    exit 0
  fi
  target_pids=("${remaining[@]}")
  /bin/sleep 1
done

/bin/kill -KILL "${target_pids[@]}" 2>/dev/null || true
/bin/sleep 1

for process_pid in "${target_pids[@]}"; do
  if /bin/kill -0 "${process_pid}" 2>/dev/null; then
    exit 1
  fi
done
