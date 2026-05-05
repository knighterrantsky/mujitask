# Runtime Watchdog Validation

## Goal

Validate the minimal controllable runtime loop:

1. A worker claims one job and writes `run_id`, `worker_id`, `worker_pid`, `started_at`, `heartbeat_at`, and `last_progress_at`.
2. Watchdog detects total timeout, heartbeat timeout, or no-progress timeout.
3. Watchdog atomically marks the running job `failed` with the current `run_id`.
4. Only after the DB update succeeds, watchdog terminates the owning worker process.
5. launchd restarts the worker and the next pending job can run.

## Start Daemons

Render and load the current launchd agents:

```bash
scripts/execution_control/install_launch_agents.sh
```

Useful manual commands:

实际 plist 名称以 `scripts/execution_control/install_launch_agents.sh` 生成结果为准。

```bash
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.<user>.mujitask.api-worker.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.<user>.mujitask.watchdog.plist"
launchctl kickstart -k "gui/$(id -u)/com.<user>.mujitask.api-worker"
launchctl kickstart -k "gui/$(id -u)/com.<user>.mujitask.watchdog"
launchctl list | grep 'mujitask'
```

The api-worker and browser-runloop plists pass `--supervisor-mode inline`.

## Submit Test Jobs

Use the runtime DB from `scripts/execution_control/executor.local.env`, then submit three API worker jobs in one waiting request:

- A: normal sleep 1 equivalent, expected `success`.
- B: stuck sleep 999 equivalent with `max_execution_seconds=10`, expected `failed/job_total_timeout`.
- C: normal sleep 1 equivalent, expected `success` after launchd restarts the worker.

The automated regression for this scenario is:

```bash
TEST_DATABASE_URL="$BUSINESS_EXECUTION_CONTROL_DB_URL" \
  ./.venv/bin/python -m pytest tests/test_runtime_watchdog_control_loop.py
```

## Observe DB

Inspect these columns on `api_worker_job`:

```sql
SELECT
  job_id,
  status,
  run_id,
  worker_id,
  worker_pid,
  started_at,
  heartbeat_at,
  last_progress_at,
  finished_at,
  error_type,
  error_code
FROM api_worker_job
ORDER BY created_at DESC
LIMIT 20;
```

Expected:

- A: `success`, non-empty `run_id`, non-zero `worker_pid`, `finished_at` set.
- B: `failed`, `error_type='timeout'`, `error_code='job_total_timeout'`, `finished_at` set.
- C: `success` with a new `worker_pid` after launchd restarts the worker.

## Observe Logs

Check:

```bash
tail -f runtime/daemons/api_worker.launchd.stderr.log
tail -f runtime/daemons/watchdog.launchd.stderr.log
tail -f runtime/daemons/watchdog.launchd.stdout.log
```

Look for:

- worker claimed `job_id/run_id` with `supervisor_mode=inline` in the JSON payload.
- watchdog detected timeout.
- watchdog marked the job failed.
- watchdog sent `SIGTERM` or `SIGKILL`.
- launchd restarted the worker with a different pid.
- the next job succeeded.

## Pass Criteria

The validation passes when:

- The stuck job is terminal `failed`.
- The failed job has `error_code='job_total_timeout'`, `worker_heartbeat_timeout`, or `job_no_progress_timeout`.
- The owning `worker_pid` is closed only after the DB failure update succeeds.
- launchd starts a new worker process.
- The next pending job is consumed and finishes successfully.
