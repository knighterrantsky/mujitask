from __future__ import annotations

import os
import socket
from dataclasses import dataclass


def _read_env(name: str, *aliases: str, default: str = "") -> str:
    for key in (name, *aliases):
        raw = os.getenv(key)
        if raw is not None and str(raw).strip():
            return str(raw)
    return default


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


def _read_float_alias(name: str, *aliases: str, default: float) -> float:
    raw = _read_env(name, *aliases, default="")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _read_bool_alias(name: str, *aliases: str, default: bool) -> bool:
    raw = _read_env(name, *aliases, default="")
    if not raw:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
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
    artifact_store_provider: str
    artifact_object_prefix: str
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_region: str
    minio_secure: bool
    minio_create_bucket: bool
    sync_referenced_files: bool
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
    lease_seconds = max(
        _read_float_alias(
            "BUSINESS_EXECUTION_CONTROL_LEASE_SECONDS",
            "EXECUTION_CONTROL_LEASE_SECONDS",
            default=120.0,
        ),
        5.0,
    )
    heartbeat_interval_seconds = _read_float_alias(
        "BUSINESS_EXECUTION_CONTROL_HEARTBEAT_INTERVAL_SECONDS",
        "EXECUTION_CONTROL_HEARTBEAT_INTERVAL_SECONDS",
        default=max(1.0, min(lease_seconds / 3.0, 30.0)),
    )
    hostname = socket.gethostname().strip() or "localhost"
    default_worker_id = f"{hostname}:{os.getpid()}"
    return ExecutionControlDefaults(
        db_url=_read_env(
            "BUSINESS_EXECUTION_CONTROL_DB_URL",
            "EXECUTION_CONTROL_DB_URL",
        ).strip(),
        db_path=_read_env(
            "BUSINESS_EXECUTION_CONTROL_DB_PATH",
            "EXECUTION_CONTROL_DB_PATH",
            default="runtime/execution_control/control_plane.sqlite3",
        ),
        artifact_root=_read_env(
            "BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT",
            "EXECUTION_CONTROL_ARTIFACT_ROOT",
            default="runtime/execution_control/object_store",
        ),
        artifact_bucket=_read_env(
            "BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET",
            "EXECUTION_CONTROL_ARTIFACT_BUCKET",
            default="local-runtime",
        ),
        artifact_store_provider=_read_env(
            "BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER",
            "EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER",
            default="local",
        ).strip().lower()
        or "local",
        artifact_object_prefix=_read_env(
            "BUSINESS_EXECUTION_CONTROL_ARTIFACT_OBJECT_PREFIX",
            "EXECUTION_CONTROL_ARTIFACT_OBJECT_PREFIX",
        ).strip().strip("/"),
        minio_endpoint=_read_env(
            "BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT",
            "EXECUTION_CONTROL_MINIO_ENDPOINT",
        ).strip(),
        minio_access_key=_read_env(
            "BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY",
            "EXECUTION_CONTROL_MINIO_ACCESS_KEY",
        ).strip(),
        minio_secret_key=_read_env(
            "BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY",
            "EXECUTION_CONTROL_MINIO_SECRET_KEY",
        ).strip(),
        minio_region=_read_env(
            "BUSINESS_EXECUTION_CONTROL_MINIO_REGION",
            "EXECUTION_CONTROL_MINIO_REGION",
        ).strip(),
        minio_secure=_read_bool_alias(
            "BUSINESS_EXECUTION_CONTROL_MINIO_SECURE",
            "EXECUTION_CONTROL_MINIO_SECURE",
            default=False,
        ),
        minio_create_bucket=_read_bool_alias(
            "BUSINESS_EXECUTION_CONTROL_MINIO_CREATE_BUCKET",
            "EXECUTION_CONTROL_MINIO_CREATE_BUCKET",
            default=False,
        ),
        sync_referenced_files=_read_bool_alias(
            "BUSINESS_EXECUTION_CONTROL_SYNC_REFERENCED_FILES",
            "EXECUTION_CONTROL_SYNC_REFERENCED_FILES",
            default=False,
        ),
        requested_by=_read_env(
            "BUSINESS_EXECUTION_CONTROL_REQUESTED_BY",
            "EXECUTION_CONTROL_REQUESTED_BY",
            default="local-cli",
        ),
        worker_id=_read_env(
            "BUSINESS_EXECUTION_CONTROL_WORKER_ID",
            "EXECUTION_CONTROL_WORKER_ID",
            default=default_worker_id,
        ),
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=max(1.0, min(heartbeat_interval_seconds, lease_seconds)),
        poll_interval_seconds=max(
            _read_float_alias(
                "BUSINESS_EXECUTION_CONTROL_POLL_INTERVAL_SECONDS",
                "EXECUTION_CONTROL_POLL_INTERVAL_SECONDS",
                default=1.0,
            ),
            0.05,
        ),
        wait_timeout_seconds=max(
            _read_float_alias(
                "BUSINESS_EXECUTION_CONTROL_WAIT_TIMEOUT_SECONDS",
                "EXECUTION_CONTROL_WAIT_TIMEOUT_SECONDS",
                default=300.0,
            ),
            1.0,
        ),
    )
