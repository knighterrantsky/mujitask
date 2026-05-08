from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any, Mapping

from automation_business_scaffold.control_plane.watchdog.models import FAIL_ACTION, WatchdogAction
from automation_business_scaffold.control_plane.watchdog.scan_queries import coerce_int


def looks_like_mujitask_worker(pid: int, *, expected_worker_id: str = "") -> bool:
    command = _process_command(pid)
    if not command:
        return False
    normalized = command.lower()
    has_project_marker = "mujitask" in normalized or "automation_business_scaffold" in normalized
    has_worker_marker = any(
        marker in normalized
        for marker in (
            "api_worker",
            "api-worker",
            "browser_worker",
            "browser-runloop",
            "browser_worker",
            "outbox",
            "daemon",
            "run_launchd_agent",
        )
    )
    if expected_worker_id and expected_worker_id.lower() in normalized:
        return True
    return has_project_marker and has_worker_marker


def kill_worker_process(
    worker_pid: int | str | None,
    *,
    expected_worker_id: str = "",
    terminate_grace_seconds: float = 5.0,
) -> dict[str, Any]:
    pid = coerce_int(worker_pid)
    if pid <= 0:
        return {"attempted": False, "killed": False, "reason": "missing_worker_pid"}
    if not _process_exists(pid):
        return {"attempted": True, "killed": True, "reason": "worker_already_exited", "worker_pid": pid}
    if not looks_like_mujitask_worker(pid, expected_worker_id=expected_worker_id):
        return {
            "attempted": True,
            "killed": False,
            "reason": "refuse_to_kill_non_mujitask_pid",
            "worker_pid": pid,
            "command": _process_command(pid),
        }

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + max(terminate_grace_seconds, 0.1)
    while time.time() < deadline:
        if not _process_exists(pid):
            return {"attempted": True, "killed": True, "signal": "SIGTERM", "worker_pid": pid}
        time.sleep(0.1)
    if _process_exists(pid):
        os.kill(pid, signal.SIGKILL)
        return {"attempted": True, "killed": True, "signal": "SIGKILL", "worker_pid": pid}
    return {"attempted": True, "killed": True, "signal": "SIGTERM", "worker_pid": pid}


def maybe_kill_timed_out_worker(action: WatchdogAction, store_result: Mapping[str, Any]) -> dict[str, Any]:
    if action.action_type != FAIL_ACTION:
        return {}
    if action.target_table not in {"api_worker_job", "task_execution"}:
        return {}
    metadata = dict(action.metadata or {})
    worker_pid = store_result.get("worker_pid") or metadata.get("observed_worker_pid")
    worker_id = str(store_result.get("worker_id") or metadata.get("observed_worker_id") or "")
    return kill_worker_process(worker_pid, expected_worker_id=worker_id)


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_command(pid: int) -> str:
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:  # noqa: BLE001
        return ""
