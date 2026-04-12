from __future__ import annotations

import os
import socket
from dataclasses import dataclass


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _read_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class BusinessDefaults:
    default_run_mode: str
    source_system: str
    target_system: str
    default_category: str
    default_price: int
    default_description: str


@dataclass(frozen=True, slots=True)
class ExecutionControlDefaults:
    db_url: str
    db_path: str
    artifact_root: str
    artifact_bucket: str
    requested_by: str
    worker_id: str
    lease_seconds: float
    heartbeat_interval_seconds: float
    poll_interval_seconds: float
    wait_timeout_seconds: float


def get_business_defaults() -> BusinessDefaults:
    return BusinessDefaults(
        default_run_mode=os.getenv("BUSINESS_DEFAULT_RUN_MODE", "draft"),
        source_system=os.getenv("BUSINESS_SOURCE_SYSTEM", "source-marketplace"),
        target_system=os.getenv("BUSINESS_TARGET_SYSTEM", "target-marketplace"),
        default_category=os.getenv("BUSINESS_DEFAULT_CATEGORY", "home"),
        default_price=_read_int("BUSINESS_DEFAULT_PRICE", 128),
        default_description=os.getenv(
            "BUSINESS_DEFAULT_DESCRIPTION",
            "Created from automation-business-scaffold.",
        ),
    )


def get_execution_control_defaults() -> ExecutionControlDefaults:
    lease_seconds = max(_read_float("BUSINESS_EXECUTION_CONTROL_LEASE_SECONDS", 120.0), 5.0)
    heartbeat_interval_seconds = _read_float(
        "BUSINESS_EXECUTION_CONTROL_HEARTBEAT_INTERVAL_SECONDS",
        max(1.0, min(lease_seconds / 3.0, 30.0)),
    )
    hostname = socket.gethostname().strip() or "localhost"
    default_worker_id = f"{hostname}:{os.getpid()}"
    return ExecutionControlDefaults(
        db_url=os.getenv("BUSINESS_EXECUTION_CONTROL_DB_URL", "").strip(),
        db_path=os.getenv(
            "BUSINESS_EXECUTION_CONTROL_DB_PATH",
            "runtime/execution_control/control_plane.sqlite3",
        ),
        artifact_root=os.getenv(
            "BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT",
            "runtime/execution_control/object_store",
        ),
        artifact_bucket=os.getenv(
            "BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET",
            "local-runtime",
        ),
        requested_by=os.getenv("BUSINESS_EXECUTION_CONTROL_REQUESTED_BY", "local-cli"),
        worker_id=os.getenv("BUSINESS_EXECUTION_CONTROL_WORKER_ID", default_worker_id),
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=max(1.0, min(heartbeat_interval_seconds, lease_seconds)),
        poll_interval_seconds=max(
            _read_float("BUSINESS_EXECUTION_CONTROL_POLL_INTERVAL_SECONDS", 1.0),
            0.05,
        ),
        wait_timeout_seconds=max(
            _read_float("BUSINESS_EXECUTION_CONTROL_WAIT_TIMEOUT_SECONDS", 300.0),
            1.0,
        ),
    )
