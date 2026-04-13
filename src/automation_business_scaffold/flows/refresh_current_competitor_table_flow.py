from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from automation_business_scaffold.config import get_execution_control_defaults
from automation_business_scaffold.flows.artifact_sync import (
    ArtifactFileSpec,
    build_artifact_payload as _build_synced_artifact_payload,
    collect_referenced_artifact_specs,
    create_store_from_settings,
    sync_artifact_specs,
)
from automation_business_scaffold.flows.execution_control_flow import build_controlled_resource_code
from automation_business_scaffold.flows.feishu_competitor_flow import (
    run_feishu_pending_rows_scan,
    run_feishu_single_row_update,
)
from automation_business_scaffold.flows.phase1_runtime_store import Phase1RuntimeStore
from automation_business_scaffold.flows.tiktok_feishu_sync_flow import run_tiktok_product_link_cleanup
from automation_business_scaffold.models import ArtifactObjectRecord

TASK_CODE = "refresh_current_competitor_table"
ITEM_CODE = "feishu_single_row_update"
WORKFLOW_CODE = "feishu_single_row_update_v1"
BROWSER_STEP_ID = "execute_browser_single_row_update"
TERMINAL_REQUEST_STATUSES = {"success", "failed", "cancelled"}


def _read_float_param(params: dict[str, Any], key: str, default: float) -> float:
    raw = params.get(key)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _read_int_param(params: dict[str, Any], key: str, default: int) -> int:
    raw = params.get(key)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _read_bool_param(params: dict[str, Any], key: str, default: bool) -> bool:
    raw = params.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    normalized = str(raw).strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _json_pretty(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _parse_reply_target(reply_target: str) -> dict[str, Any]:
    raw_value = str(reply_target or "").strip()
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except Exception:
        try:
            payload = ast.literal_eval(raw_value)
        except Exception:
            return {"to": raw_value}
    return payload if isinstance(payload, dict) else {"to": raw_value}


def _resolve_openclaw_cli() -> str:
    configured = str(os.environ.get("OPENCLAW_CLI_BIN", "")).strip()
    if configured:
        return configured
    resolved = shutil.which("openclaw")
    if resolved:
        return resolved
    for candidate in ("/opt/homebrew/bin/openclaw", "/usr/local/bin/openclaw"):
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("Cannot find the openclaw CLI. Set OPENCLAW_CLI_BIN or add openclaw to PATH.")


def _dispatch_via_openclaw_message(*, message_text: str, reply_target: str) -> dict[str, Any]:
    delivery_context = _parse_reply_target(reply_target)
    channel = str(delivery_context.get("channel", "") or "").strip()
    target = str(
        delivery_context.get("to")
        or delivery_context.get("target")
        or delivery_context.get("reply_to")
        or ""
    ).strip()
    account_id = str(
        delivery_context.get("accountId")
        or delivery_context.get("account_id")
        or ""
    ).strip()
    if not channel or not target:
        raise RuntimeError("Outbox delivery is missing reply_target.channel or reply_target.to.")

    command = [
        _resolve_openclaw_cli(),
        "message",
        "send",
        "--channel",
        channel,
        "--target",
        target,
        "--message",
        message_text,
        "--json",
    ]
    if account_id:
        command.extend(["--account", account_id])

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=max(float(os.environ.get("OPENCLAW_MESSAGE_SEND_TIMEOUT_SECONDS", "20") or 20.0), 1.0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"openclaw message send timed out: {exc.cmd}") from exc
    if completed.returncode != 0:
        stderr = str(completed.stderr or "").strip()
        stdout = str(completed.stdout or "").strip()
        details = stderr or stdout or f"exit code {completed.returncode}"
        raise RuntimeError(f"openclaw message send failed: {details}")

    stdout = str(completed.stdout or "").strip()
    if not stdout:
        return {}
    try:
        payload = json.loads(stdout)
    except Exception:
        payload = {"raw_stdout": stdout}
    return payload if isinstance(payload, dict) else {"raw_stdout": stdout}


def _openclaw_config_path() -> Path:
    configured = str(os.environ.get("OPENCLAW_CONFIG_PATH", "")).strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".openclaw" / "openclaw.json"


def _load_openclaw_feishu_account_config(account_id: str) -> dict[str, str]:
    config_path = _openclaw_config_path()
    if not config_path.exists():
        raise RuntimeError(f"OpenClaw config file not found: {config_path}")
    try:
        config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"OpenClaw config file is not valid JSON: {config_path}") from exc
    if not isinstance(config_payload, dict):
        raise RuntimeError(f"OpenClaw config file does not contain a JSON object: {config_path}")
    channels = config_payload.get("channels")
    if not isinstance(channels, dict):
        raise RuntimeError("OpenClaw config is missing channels.feishu.")
    feishu_config = channels.get("feishu")
    if not isinstance(feishu_config, dict):
        raise RuntimeError("OpenClaw config is missing channels.feishu.")
    configured_account_id = account_id or str(feishu_config.get("defaultAccount", "") or "default").strip() or "default"
    accounts = feishu_config.get("accounts")
    if isinstance(accounts, dict) and accounts:
        account_config = accounts.get(configured_account_id)
        if not isinstance(account_config, dict):
            raise RuntimeError(f"OpenClaw config does not define Feishu account '{configured_account_id}'.")
    else:
        account_config = feishu_config
    app_id = str(account_config.get("appId") or feishu_config.get("appId") or "").strip()
    app_secret = str(account_config.get("appSecret") or feishu_config.get("appSecret") or "").strip()
    domain = str(account_config.get("domain") or feishu_config.get("domain") or "feishu").strip().lower()
    if not app_id or not app_secret:
        raise RuntimeError(f"Feishu account '{configured_account_id}' is missing appId/appSecret in OpenClaw config.")
    return {
        "account_id": configured_account_id,
        "app_id": app_id,
        "app_secret": app_secret,
        "domain": domain or "feishu",
    }


def _feishu_base_url(domain: str) -> str:
    normalized = str(domain or "feishu").strip().lower()
    if normalized == "lark":
        return "https://open.larksuite.com"
    return "https://open.feishu.cn"


def _post_json(url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request_headers = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    timeout_seconds = max(float(os.environ.get("FEISHU_BOT_API_TIMEOUT_SECONDS", "15") or 15.0), 1.0)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
    except Exception as exc:
        raise RuntimeError(f"Request failed for {url}: {exc}") from exc
    try:
        response_payload = json.loads(body)
    except Exception as exc:
        raise RuntimeError(f"Response from {url} is not valid JSON: {body}") from exc
    if not isinstance(response_payload, dict):
        raise RuntimeError(f"Response from {url} is not a JSON object.")
    return response_payload


def _read_response_code(payload: dict[str, Any]) -> int:
    raw_code = payload.get("code", -1)
    try:
        return int(raw_code)
    except (TypeError, ValueError):
        return -1


def _normalize_feishu_receive_target(raw_target: str) -> tuple[str, str]:
    normalized = str(raw_target or "").strip()
    if not normalized:
        raise RuntimeError("Feishu reply target is empty.")
    lowered = normalized.lower()
    if lowered.startswith("user:"):
        return "open_id", normalized.split(":", 1)[1]
    if lowered.startswith("dm:"):
        return "open_id", normalized.split(":", 1)[1]
    if lowered.startswith("open_id:"):
        return "open_id", normalized.split(":", 1)[1]
    if lowered.startswith("chat:"):
        return "chat_id", normalized.split(":", 1)[1]
    if lowered.startswith("group:"):
        return "chat_id", normalized.split(":", 1)[1]
    if lowered.startswith("channel:"):
        return "chat_id", normalized.split(":", 1)[1]
    if normalized.startswith("oc_"):
        return "chat_id", normalized
    return "open_id", normalized


def _dispatch_via_feishu_bot_api(*, message_text: str, reply_target: str) -> dict[str, Any]:
    delivery_context = _parse_reply_target(reply_target)
    channel = str(delivery_context.get("channel", "") or "").strip().lower()
    if channel and channel != "feishu":
        raise RuntimeError(f"feishu_bot_api only supports Feishu reply targets, got '{channel}'.")
    raw_target = str(delivery_context.get("to") or delivery_context.get("target") or "").strip()
    account_id = str(
        delivery_context.get("accountId")
        or delivery_context.get("account_id")
        or ""
    ).strip()
    receive_id_type, receive_id = _normalize_feishu_receive_target(raw_target)
    feishu_account = _load_openclaw_feishu_account_config(account_id)
    base_url = _feishu_base_url(feishu_account["domain"])

    token_payload = _post_json(
        f"{base_url}/open-apis/auth/v3/tenant_access_token/internal",
        {
            "app_id": feishu_account["app_id"],
            "app_secret": feishu_account["app_secret"],
        },
    )
    if _read_response_code(token_payload) != 0:
        raise RuntimeError(f"Feishu tenant_access_token request failed: {token_payload}")
    tenant_access_token = str(token_payload.get("tenant_access_token") or "").strip()
    if not tenant_access_token:
        raise RuntimeError("Feishu tenant_access_token response did not include tenant_access_token.")

    message_payload = _post_json(
        f"{base_url}/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
        {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": message_text}, ensure_ascii=False),
            "uuid": uuid.uuid4().hex,
        },
        headers={"Authorization": f"Bearer {tenant_access_token}"},
    )
    if _read_response_code(message_payload) != 0:
        raise RuntimeError(f"Feishu send message request failed: {message_payload}")
    return message_payload


def _sanitize_task_payload(params: dict[str, Any]) -> dict[str, Any]:
    control_keys = {
        "control_action",
        "request_id",
        "execution_id",
        "execution_control_db_url",
        "execution_control_db_path",
        "execution_requested_by",
        "execution_worker_id",
        "execution_lease_seconds",
        "execution_heartbeat_interval_seconds",
        "execution_poll_interval_seconds",
        "execution_wait_timeout_seconds",
        "execution_retry_delay_seconds",
        "execution_control_max_iterations",
        "execution_control_max_idle_cycles",
        "execution_control_stop_when_idle",
        "execution_control_artifact_root",
        "execution_control_artifact_bucket",
        "execution_control_artifact_store_provider",
        "execution_control_artifact_object_prefix",
        "execution_control_minio_endpoint",
        "execution_control_minio_access_key",
        "execution_control_minio_secret_key",
        "execution_control_minio_region",
        "execution_control_minio_secure",
        "execution_control_minio_create_bucket",
        "execution_control_sync_referenced_files",
        "idempotency_key",
        "notification_channel_code",
        "source_channel_code",
        "source_session_id",
        "reply_target",
        "trigger_mode",
        "requested_by",
    }
    return {key: value for key, value in params.items() if key not in control_keys}


def _phase1_settings(params: dict[str, Any]) -> dict[str, Any]:
    defaults = get_execution_control_defaults()
    configured_db_url = str(params.get("execution_control_db_url") or defaults.db_url).strip()
    configured_db_path = str(params.get("execution_control_db_path") or defaults.db_path)
    if not configured_db_url and "://" in configured_db_path:
        configured_db_url = configured_db_path
        configured_db_path = defaults.db_path
    requested_by = str(
        params.get("requested_by")
        or params.get("execution_requested_by")
        or defaults.requested_by
    ).strip()
    notification_channel_code = str(
        params.get("notification_channel_code")
        or params.get("source_channel_code")
        or "noop"
    ).strip()
    artifact_store_provider = str(
        params.get("execution_control_artifact_store_provider") or defaults.artifact_store_provider
    ).strip().lower() or "local"
    if "execution_control_sync_referenced_files" in params:
        sync_referenced_files = _read_bool_param(
            params,
            "execution_control_sync_referenced_files",
            defaults.sync_referenced_files,
        )
    else:
        sync_referenced_files = bool(defaults.sync_referenced_files or artifact_store_provider != "local")
    return {
        "db_url": configured_db_url,
        "db_path": configured_db_path,
        "artifact_root": str(params.get("execution_control_artifact_root") or defaults.artifact_root),
        "artifact_bucket": str(
            params.get("execution_control_artifact_bucket") or defaults.artifact_bucket
        ),
        "artifact_store_provider": artifact_store_provider,
        "artifact_object_prefix": str(
            params.get("execution_control_artifact_object_prefix") or defaults.artifact_object_prefix
        ),
        "minio_endpoint": str(
            params.get("execution_control_minio_endpoint") or defaults.minio_endpoint
        ),
        "minio_access_key": str(
            params.get("execution_control_minio_access_key") or defaults.minio_access_key
        ),
        "minio_secret_key": str(
            params.get("execution_control_minio_secret_key") or defaults.minio_secret_key
        ),
        "minio_region": str(
            params.get("execution_control_minio_region") or defaults.minio_region
        ),
        "minio_secure": _read_bool_param(
            params,
            "execution_control_minio_secure",
            defaults.minio_secure,
        ),
        "minio_create_bucket": _read_bool_param(
            params,
            "execution_control_minio_create_bucket",
            defaults.minio_create_bucket,
        ),
        "sync_referenced_files": sync_referenced_files,
        "requested_by": requested_by or defaults.requested_by,
        "worker_id": str(params.get("execution_worker_id") or defaults.worker_id),
        "trigger_mode": str(params.get("trigger_mode") or "manual"),
        "source_channel_code": notification_channel_code or "noop",
        "source_session_id": str(params.get("source_session_id") or ""),
        "reply_target": str(params.get("reply_target") or ""),
        "lease_seconds": max(
            _read_float_param(params, "execution_lease_seconds", defaults.lease_seconds),
            5.0,
        ),
        "heartbeat_interval_seconds": max(
            _read_float_param(
                params,
                "execution_heartbeat_interval_seconds",
                defaults.heartbeat_interval_seconds,
            ),
            0.2,
        ),
        "poll_interval_seconds": max(
            _read_float_param(params, "execution_control_poll_interval_seconds", defaults.poll_interval_seconds),
            0.05,
        ),
        "wait_timeout_seconds": max(
            _read_float_param(params, "execution_wait_timeout_seconds", defaults.wait_timeout_seconds),
            1.0,
        ),
        "retry_delay_seconds": max(
            _read_float_param(params, "execution_retry_delay_seconds", 15.0),
            0.1,
        ),
    }


def _create_store(settings: dict[str, Any]) -> Phase1RuntimeStore:
    return Phase1RuntimeStore(
        db_url=str(settings.get("db_url", "") or ""),
        db_path=str(settings.get("db_path", "") or ""),
    )


def _sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json_file(path: Path, payload: Any) -> None:
    _write_text(path, f"{_json_pretty(payload)}\n")


def _artifact_content_type(kind: str, path: Path) -> str:
    if kind.endswith("_json") or path.suffix == ".json":
        return "application/json"
    if kind.endswith("_log") or path.suffix == ".log":
        return "text/plain"
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _runtime_object_key(run_id: str, relative_name: str) -> str:
    return f"runs/{run_id}/{relative_name}"


def _build_artifact_record(
    *,
    run_id: str,
    step_id: str,
    kind: str,
    bucket: str,
    object_key: str,
    path: Path,
    created_at: float,
) -> ArtifactObjectRecord:
    return ArtifactObjectRecord(
        artifact_id=uuid.uuid4().hex,
        run_id=run_id,
        step_id=step_id,
        kind=kind,
        bucket=bucket,
        object_key=object_key,
        etag=_sha256_of_file(path),
        size=path.stat().st_size,
        content_type=_artifact_content_type(kind, path),
        source_path=str(path.resolve()),
        created_at=created_at,
    )


def _artifact_payload_from_records(
    *,
    artifact_root: Path,
    run_id: str,
    records: list[ArtifactObjectRecord],
) -> dict[str, Any]:
    by_kind = {record.kind: record for record in records}
    run_prefix = artifact_root / "runs" / run_id
    return {
        "artifact_count": len(records),
        "artifacts": [record.to_dict() for record in records],
        "artifact_uri_prefix": run_prefix.resolve().as_uri() if records else "",
        "run_object_key": by_kind.get("run_json").object_key if "run_json" in by_kind else "",
        "steps_object_key": by_kind.get("steps_json").object_key if "steps_json" in by_kind else "",
        "signals_object_key": by_kind.get("signals_json").object_key if "signals_json" in by_kind else "",
        "stdout_object_key": by_kind.get("stdout_log").object_key if "stdout_log" in by_kind else "",
        "artifacts_dir": str((run_prefix / "artifacts").resolve()) if records else "",
    }


class _TeeStream:
    def __init__(self, *targets: Any):
        self._targets = targets

    def write(self, data: str) -> int:
        for target in self._targets:
            target.write(data)
            target.flush()
        return len(data)

    def flush(self) -> None:
        for target in self._targets:
            target.flush()


class _LeaseHeartbeat:
    def __init__(
        self,
        *,
        store: Phase1RuntimeStore,
        execution_id: str,
        lease_seconds: float,
        interval_seconds: float,
    ):
        self._store = store
        self._execution_id = execution_id
        self._lease_seconds = lease_seconds
        self._interval_seconds = max(min(interval_seconds, lease_seconds), 0.1)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                self._store.heartbeat_browser_execution(
                    execution_id=self._execution_id,
                    lease_seconds=self._lease_seconds,
                )
            except Exception:
                return

    def __enter__(self) -> "_LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop_event.set()
        self._thread.join(timeout=self._interval_seconds + 1.0)


def _extract_result_item(payload: dict[str, Any]) -> dict[str, Any]:
    item = payload.get("item")
    if isinstance(item, dict) and item:
        return item
    items = payload.get("items")
    if isinstance(items, list) and items:
        first_item = items[0]
        if isinstance(first_item, dict):
            return first_item
    return {}


def _classify_execution_result(payload: dict[str, Any]) -> str:
    item = _extract_result_item(payload)
    status = str(item.get("status", "") or "").strip()
    if status.startswith("skipped_"):
        return "skipped"
    if status.endswith("_failed") or status in {"update_failed", "failed"}:
        return "failed"
    return "success"


def _build_enqueue_items(request_payload: dict[str, Any], target_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resource_code = build_controlled_resource_code(request_payload)
    profile_ref = str(request_payload.get("profile_ref", "") or "main").strip() or "main"
    items: list[dict[str, Any]] = []
    for row in target_rows:
        record_id = str(row.get("record_id", "") or "").strip()
        if not record_id:
            continue
        payload = dict(request_payload)
        payload["record_id"] = record_id
        if row.get("source_url"):
            payload["source_url"] = row.get("source_url")
        if row.get("sku_id"):
            payload["sku_id"] = row.get("sku_id")
        items.append(
            {
                "business_key": record_id,
                "dedupe_key": f"{ITEM_CODE}:{profile_ref}:{record_id}",
                "resource_code": resource_code,
                "max_attempts": 3,
                "payload": payload,
            }
        )
    return items


def _summary_counts_only(mapping: dict[str, Any]) -> dict[str, int]:
    counts = mapping.get("counts", {}) if isinstance(mapping, dict) else {}
    if not isinstance(counts, dict):
        return {}
    normalized: dict[str, int] = {}
    for key, value in counts.items():
        try:
            normalized[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return normalized


def _build_live_summary(request: Any, executions: list[Any]) -> dict[str, Any]:
    stage_cursor = request.stage_cursor if isinstance(request.stage_cursor, dict) else {}
    scan_payload = stage_cursor.get("scan", {})
    scan_target_rows = scan_payload.get("target_rows", []) if isinstance(scan_payload, dict) else []
    enqueue_payload = stage_cursor.get("enqueue", {})
    enqueue_skipped_count = (
        int(enqueue_payload.get("skipped_count", 0) or 0)
        if isinstance(enqueue_payload, dict)
        else 0
    )
    total = len(scan_target_rows) if isinstance(scan_target_rows, list) else 0
    total = max(total, int(request.child_total_count or 0) + enqueue_skipped_count)
    counts: dict[str, int] = {}
    if int(request.child_success_count or 0) > 0:
        counts["success"] = int(request.child_success_count)
    if int(request.child_failed_count or 0) > 0:
        counts["failed"] = int(request.child_failed_count)
    if int(request.child_skipped_count or 0) > 0:
        counts["skipped"] = int(request.child_skipped_count)
    active_count = max(int(request.child_total_count or 0) - int(request.child_terminal_count or 0), 0)
    if request.status in {"pending", "running", "waiting_children", "ready_for_summary"} and active_count > 0:
        counts["active"] = active_count
    if enqueue_skipped_count > 0:
        counts["deduped_active"] = enqueue_skipped_count
    if request.status == "pending" and not counts:
        counts["queued"] = 1
        total = max(total, 1)
    return {"total": total, "counts": counts}


def _build_result_payload(request: Any, executions: list[Any], outbox_records: list[Any]) -> dict[str, Any]:
    stage_cursor = request.stage_cursor if isinstance(request.stage_cursor, dict) else {}
    item_results: list[dict[str, Any]] = []
    failed_items: list[dict[str, Any]] = []
    for execution in executions:
        item = _extract_result_item(execution.result if isinstance(execution.result, dict) else {})
        if not item:
            continue
        enriched_item = dict(item)
        enriched_item.setdefault("execution_id", execution.execution_id)
        enriched_item.setdefault("execution_status", execution.status)
        item_results.append(enriched_item)
        if execution.status == "failed":
            failed_items.append(enriched_item)
    return {
        "task_request": request.to_dict(),
        "cleanup": stage_cursor.get("cleanup", {}),
        "scan": stage_cursor.get("scan", {}),
        "enqueue": stage_cursor.get("enqueue", {}),
        "executions": [execution.to_dict() for execution in executions],
        "outbox": [record.to_dict() for record in outbox_records],
        "items": item_results,
        "failed_items": failed_items,
    }


def _build_outbox_text(request: Any, summary: dict[str, Any]) -> str:
    counts = _summary_counts_only(summary)
    total = int(summary.get("total", 0) or 0) if isinstance(summary, dict) else 0
    parts = [f"任务 {request.request_id} 已完成"]
    if total > 0:
        parts.append(f"目标 {total} 条")
    if counts:
        parts.append(
            "，".join(f"{key}={value}" for key, value in sorted(counts.items()))
        )
    return "；".join(parts)


def _build_request_payload(
    *,
    store: Phase1RuntimeStore,
    request_id: str,
    control_action: str,
    message: str,
) -> dict[str, Any]:
    request = store.load_task_request(request_id=request_id)
    executions = store.list_task_executions(request_id=request_id)
    outbox_records = store.list_request_outbox(request_id=request_id)
    summary = request.summary if request.summary else _build_live_summary(request, executions)
    result = request.result if request.result else _build_result_payload(request, executions, outbox_records)
    payload = {
        "control_action": control_action,
        "message": message,
        "task_code": request.task_code,
        "request_id": request.request_id,
        "request_status": request.status,
        "current_stage": request.current_stage,
        "summary": summary,
        "result": result,
        "request": request.to_dict(),
        "executions": [execution.to_dict() for execution in executions],
        "outbox": [record.to_dict() for record in outbox_records],
        "child_total_count": request.child_total_count,
        "child_terminal_count": request.child_terminal_count,
        "child_success_count": request.child_success_count,
        "child_failed_count": request.child_failed_count,
        "child_skipped_count": request.child_skipped_count,
        "error": request.error_text,
        "item": {
            "request_id": request.request_id,
            "status": request.status,
            "current_stage": request.current_stage,
        },
        "items": result.get("items", []) if isinstance(result, dict) else [],
    }
    if summary.get("total", 0) == 0 and control_action == "submit":
        payload["summary"] = {"total": 1, "counts": {"queued": 1}}
    return payload


def _sync_browser_artifacts(
    *,
    store: Phase1RuntimeStore,
    settings: dict[str, Any],
    execution: Any,
    result_payload: dict[str, Any],
    error_text: str,
    stdout_path: Path,
) -> dict[str, Any]:
    artifact_root = Path(str(settings["artifact_root"])).expanduser()
    run_root = artifact_root / "runs" / execution.run_id
    artifacts_root = run_root / "artifacts" / BROWSER_STEP_ID
    created_at = time.time()

    run_payload = {
        "run_id": execution.run_id,
        "request_id": execution.request_id,
        "execution_id": execution.execution_id,
        "status": execution.status,
        "worker_id": execution.worker_id,
        "resource_code": execution.resource_code,
        "summary": execution.summary,
        "result": result_payload,
        "error": error_text,
        "created_at": execution.created_at,
        "started_at": execution.started_at,
        "finished_at": execution.finished_at,
    }
    steps_payload = [
        {
            "step_id": BROWSER_STEP_ID,
            "status": execution.status,
            "started_at": execution.started_at,
            "finished_at": execution.finished_at,
            "artifacts": {
                "state_dump": str((artifacts_root / "state.json").resolve()),
            },
            "summary": execution.summary,
            "error": error_text,
        }
    ]
    signals_payload = [
        {
            "signal_type": "execution.claimed",
            "execution_id": execution.execution_id,
            "run_id": execution.run_id,
            "at": execution.started_at,
        },
        {
            "signal_type": "step.completed" if execution.status != "failed" else "step.failed",
            "execution_id": execution.execution_id,
            "step_id": BROWSER_STEP_ID,
            "run_id": execution.run_id,
            "at": execution.finished_at,
        },
    ]
    state_payload = {
        "execution": execution.to_dict(),
        "result": result_payload,
        "error": error_text,
    }

    run_file = run_root / "run.json"
    steps_file = run_root / "steps.json"
    signals_file = run_root / "signals.json"
    state_file = artifacts_root / "state.json"

    _write_json_file(run_file, run_payload)
    _write_json_file(steps_file, steps_payload)
    _write_json_file(signals_file, signals_payload)
    _write_json_file(state_file, state_payload)

    specs = [
        ArtifactFileSpec(
            kind="run_json",
            step_id=BROWSER_STEP_ID,
            relative_name="run.json",
            path=run_file,
        ),
        ArtifactFileSpec(
            kind="steps_json",
            step_id=BROWSER_STEP_ID,
            relative_name="steps.json",
            path=steps_file,
        ),
        ArtifactFileSpec(
            kind="signals_json",
            step_id=BROWSER_STEP_ID,
            relative_name="signals.json",
            path=signals_file,
        ),
        ArtifactFileSpec(
            kind="stdout_log",
            step_id=BROWSER_STEP_ID,
            relative_name="stdout.log",
            path=stdout_path,
        ),
        ArtifactFileSpec(
            kind="state_json",
            step_id=BROWSER_STEP_ID,
            relative_name=f"artifacts/{BROWSER_STEP_ID}/state.json",
            path=state_file,
        ),
    ]
    if bool(settings.get("sync_referenced_files", False)):
        specs.extend(
            collect_referenced_artifact_specs(
                result_payload=result_payload,
                step_id=BROWSER_STEP_ID,
            )
        )
    artifact_store = create_store_from_settings(settings)
    records, artifact_uri_prefix = sync_artifact_specs(
        run_id=execution.run_id,
        request_id=execution.request_id,
        execution_id=execution.execution_id,
        artifact_root=artifact_root,
        artifact_bucket=str(settings["artifact_bucket"]),
        artifact_object_prefix=str(settings.get("artifact_object_prefix", "") or ""),
        specs=specs,
        artifact_store=artifact_store,
        created_at=created_at,
    )
    store.replace_artifacts(run_id=execution.run_id, records=records)
    return _build_synced_artifact_payload(
        artifact_root=artifact_root,
        run_id=execution.run_id,
        records=records,
        artifact_uri_prefix=artifact_uri_prefix,
    )


def _fail_request(
    *,
    store: Phase1RuntimeStore,
    request_id: str,
    current_stage: str,
    error_text: str,
) -> dict[str, Any]:
    request = store.update_task_request(
        request_id=request_id,
        status="failed",
        current_stage=current_stage,
        error_text=error_text,
        summary={"total": 1, "counts": {"failed": 1}},
        result={"error": error_text},
        finished_at=time.time(),
    )
    store.create_notification_outbox(
        channel_code=request.source_channel_code or "noop",
        event_type="task_request.completed",
        ref_id=request.request_id,
        reply_target=request.reply_target,
        payload={
            "message_text": f"任务 {request.request_id} 执行失败",
            "request_id": request.request_id,
            "summary": request.summary,
            "error": error_text,
        },
        dedupe_key=f"task_request.completed:{request.request_id}",
    )
    return _build_request_payload(
        store=store,
        request_id=request.request_id,
        control_action="executor_once",
        message="Top-level executor marked the request as failed.",
    )


def _finalize_request_summary(
    *,
    store: Phase1RuntimeStore,
    request_id: str,
) -> dict[str, Any]:
    request = store.load_task_request(request_id=request_id)
    executions = store.list_task_executions(request_id=request_id)
    outbox_records = store.list_request_outbox(request_id=request_id)
    provisional_summary = _build_live_summary(request, executions)
    result = _build_result_payload(request, executions, outbox_records)
    final_request = store.update_task_request(
        request_id=request_id,
        status="success",
        current_stage="completed",
        summary=provisional_summary,
        result=result,
        error_text="",
        finished_at=time.time(),
    )
    summary_outbox = store.create_notification_outbox(
        channel_code=final_request.source_channel_code or "noop",
        event_type="task_request.completed",
        ref_id=final_request.request_id,
        reply_target=final_request.reply_target,
        payload={
            "message_text": _build_outbox_text(final_request, provisional_summary),
            "request_id": final_request.request_id,
            "task_code": final_request.task_code,
            "summary": provisional_summary,
            "result": result,
        },
        dedupe_key=f"task_request.completed:{final_request.request_id}",
    )
    result["outbox"] = [summary_outbox.to_dict()]
    store.update_task_request(request_id=request_id, result=result)
    return _build_request_payload(
        store=store,
        request_id=request_id,
        control_action="executor_once",
        message="Top-level executor finalized the task summary and queued one notification.",
    )


def submit_refresh_current_competitor_table(params: dict[str, Any]) -> dict[str, Any]:
    settings = _phase1_settings(params)
    store = _create_store(settings)
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code=TASK_CODE,
        payload=_sanitize_task_payload(params),
        requested_by=str(settings["requested_by"]),
        trigger_mode=str(settings["trigger_mode"]),
        source_channel_code=str(settings["source_channel_code"]),
        source_session_id=str(settings["source_session_id"]),
        reply_target=str(settings["reply_target"]),
        idempotency_key=str(params.get("idempotency_key", "") or "").strip(),
    )
    return _build_request_payload(
        store=store,
        request_id=request.request_id,
        control_action="submit",
        message="Top-level refresh task accepted.",
    )


def get_refresh_current_competitor_table_status(params: dict[str, Any]) -> dict[str, Any]:
    settings = _phase1_settings(params)
    store = _create_store(settings)
    return _build_request_payload(
        store=store,
        request_id=str(params.get("request_id", "") or ""),
        control_action="status",
        message="Loaded top-level refresh task status.",
    )


def execute_phase1_executor_once(params: dict[str, Any]) -> dict[str, Any]:
    settings = _phase1_settings(params)
    store = _create_store(settings)
    claimed_request = store.claim_next_task_request(worker_id=str(settings["worker_id"]))
    if claimed_request is None:
        return {
            "control_action": "executor_once",
            "daemon_status": "idle",
            "processed_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "message": "No top-level task_request is ready for executor processing.",
            "summary": {"total": 0, "counts": {}},
            "item": {},
            "items": [],
            "request_id": "",
            "request_status": "idle",
            "current_stage": "",
            "outbox": [],
        }

    current_stage = str(claimed_request.current_stage or "").strip()
    if current_stage in {"", "submitted"}:
        try:
            request_payload = dict(claimed_request.payload)
            store.update_task_request(
                request_id=claimed_request.request_id,
                current_stage="cleanup",
                started_at=claimed_request.started_at or time.time(),
            )
            cleanup_payload = run_tiktok_product_link_cleanup(request_payload)
            store.update_task_request(
                request_id=claimed_request.request_id,
                current_stage="pending_rows_scan",
            )
            scan_payload = run_feishu_pending_rows_scan(request_payload)
            target_rows = scan_payload.get("target_rows", [])
            if not isinstance(target_rows, list):
                target_rows = []
            enqueue_payload = store.enqueue_task_executions(
                request_id=claimed_request.request_id,
                item_code=ITEM_CODE,
                workflow_code=WORKFLOW_CODE,
                items=_build_enqueue_items(request_payload, target_rows),
            )
            stage_cursor = {
                "cleanup": cleanup_payload,
                "scan": scan_payload,
                "enqueue": enqueue_payload,
            }
            child_total_count = int(enqueue_payload.get("created_count", 0) or 0)
            if child_total_count > 0:
                store.update_task_request(
                    request_id=claimed_request.request_id,
                    status="waiting_children",
                    current_stage="waiting_children",
                    stage_cursor=stage_cursor,
                    error_text="",
                )
                payload = _build_request_payload(
                    store=store,
                    request_id=claimed_request.request_id,
                    control_action="executor_once",
                    message="Top-level executor planned cleanup/scan and queued browser leaf tasks.",
                )
                payload.update(
                    {
                        "daemon_status": "processed",
                        "processed_count": 1,
                        "success_count": 1,
                        "failed_count": 0,
                    }
                )
                return payload
            store.update_task_request(
                request_id=claimed_request.request_id,
                status="ready_for_summary",
                current_stage="ready_for_summary",
                stage_cursor=stage_cursor,
                error_text="",
            )
            payload = _finalize_request_summary(store=store, request_id=claimed_request.request_id)
            payload.update(
                {
                    "daemon_status": "processed",
                    "processed_count": 1,
                    "success_count": 1,
                    "failed_count": 0,
                }
            )
            return payload
        except Exception as exc:
            error_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            payload = _fail_request(
                store=store,
                request_id=claimed_request.request_id,
                current_stage="failed",
                error_text=error_text,
            )
            payload.update(
                {
                    "daemon_status": "processed",
                    "processed_count": 1,
                    "success_count": 0,
                    "failed_count": 1,
                }
            )
            return payload

    if current_stage == "ready_for_summary":
        try:
            payload = _finalize_request_summary(store=store, request_id=claimed_request.request_id)
            payload.update(
                {
                    "daemon_status": "processed",
                    "processed_count": 1,
                    "success_count": 1,
                    "failed_count": 0,
                }
            )
            return payload
        except Exception as exc:
            error_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            payload = _fail_request(
                store=store,
                request_id=claimed_request.request_id,
                current_stage="failed",
                error_text=error_text,
            )
            payload.update(
                {
                    "daemon_status": "processed",
                    "processed_count": 1,
                    "success_count": 0,
                    "failed_count": 1,
                }
            )
            return payload

    return _build_request_payload(
        store=store,
        request_id=claimed_request.request_id,
        control_action="executor_once",
        message=f"Top-level request is already at stage {current_stage}.",
    )


def _run_browser_execution_once(
    *,
    store: Phase1RuntimeStore,
    settings: dict[str, Any],
    execution: Any,
) -> tuple[Any, dict[str, Any]]:
    run_id = str(execution.run_id or f"managed-{execution.execution_id}")
    artifact_root = Path(str(settings["artifact_root"])).expanduser()
    stdout_path = artifact_root / "runs" / run_id / "stdout.log"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)

    result_payload: dict[str, Any] = {}
    error_text = ""
    finalized_execution = execution

    try:
        with stdout_path.open("w", encoding="utf-8") as stdout_handle:
            tee_stdout = _TeeStream(sys.stdout, stdout_handle)
            tee_stderr = _TeeStream(sys.stderr, stdout_handle)
            with contextlib.redirect_stdout(tee_stdout), contextlib.redirect_stderr(tee_stderr):
                print(
                    f"[phase1-browser] execution_id={execution.execution_id} "
                    f"request_id={execution.request_id} run_id={run_id} status=running"
                )
                with _LeaseHeartbeat(
                    store=store,
                    execution_id=execution.execution_id,
                    lease_seconds=float(settings["lease_seconds"]),
                    interval_seconds=float(settings["heartbeat_interval_seconds"]),
                ):
                    result_payload = run_feishu_single_row_update(dict(execution.payload))
                terminal_status = _classify_execution_result(result_payload)
                if terminal_status == "success":
                    finalized_execution = store.mark_browser_execution_success(
                        execution_id=execution.execution_id,
                        run_id=run_id,
                        summary=result_payload.get("summary", {}),
                        result=result_payload,
                    )
                elif terminal_status == "skipped":
                    finalized_execution = store.mark_browser_execution_skipped(
                        execution_id=execution.execution_id,
                        run_id=run_id,
                        summary=result_payload.get("summary", {}),
                        result=result_payload,
                    )
                else:
                    finalized_execution = store.mark_browser_execution_retry_or_failed(
                        execution_id=execution.execution_id,
                        run_id=run_id,
                        error_text=str(_extract_result_item(result_payload).get("error", "") or "Leaf execution failed."),
                        summary=result_payload.get("summary", {}),
                        result=result_payload,
                        retry_delay_seconds=float(settings["retry_delay_seconds"]),
                    )
                print(
                    f"[phase1-browser] execution_id={execution.execution_id} "
                    f"run_id={run_id} status={finalized_execution.status}"
                )
    except Exception as exc:
        error_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        finalized_execution = store.mark_browser_execution_retry_or_failed(
            execution_id=execution.execution_id,
            run_id=run_id,
            error_text=error_text,
            summary=result_payload.get("summary", {}) if isinstance(result_payload, dict) else {},
            result=result_payload if isinstance(result_payload, dict) else {},
            retry_delay_seconds=float(settings["retry_delay_seconds"]),
        )
        with stdout_path.open("a", encoding="utf-8") as stdout_handle:
            stdout_handle.write(f"\n[phase1-browser] execution failed\n{error_text}\n")

    if finalized_execution.status in {"success", "skipped", "failed"}:
        artifact_payload = _sync_browser_artifacts(
            store=store,
            settings=settings,
            execution=finalized_execution,
            result_payload=result_payload if isinstance(result_payload, dict) else {},
            error_text=error_text,
            stdout_path=stdout_path,
        )
    else:
        artifact_payload = {
            "artifact_count": 0,
            "artifacts": [],
            "artifact_uri_prefix": "",
            "run_object_key": "",
            "steps_object_key": "",
            "signals_object_key": "",
            "stdout_object_key": "",
            "artifacts_dir": "",
        }
    return finalized_execution, artifact_payload


def execute_phase1_browser_once(params: dict[str, Any]) -> dict[str, Any]:
    settings = _phase1_settings(params)
    store = _create_store(settings)
    execution = store.claim_next_browser_execution(
        worker_id=str(settings["worker_id"]),
        lease_seconds=float(settings["lease_seconds"]),
    )
    if execution is None:
        return {
            "control_action": "browser_once",
            "daemon_status": "idle",
            "processed_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "message": "No browser leaf execution is ready to run.",
            "summary": {"total": 0, "counts": {}},
            "item": {},
            "items": [],
            "request_id": "",
            "execution_id": "",
            "execution_status": "idle",
        }
    finalized_execution, artifact_payload = _run_browser_execution_once(
        store=store,
        settings=settings,
        execution=execution,
    )
    payload = {
        "control_action": "browser_once",
        "daemon_status": "processed",
        "processed_count": 1,
        "success_count": 1 if finalized_execution.status in {"success", "skipped"} else 0,
        "failed_count": 1 if finalized_execution.status == "failed" else 0,
        "message": "Browser runloop processed one leaf execution.",
        "request_id": finalized_execution.request_id,
        "execution_id": finalized_execution.execution_id,
        "execution_status": finalized_execution.status,
        "run_id": finalized_execution.run_id,
        "summary": finalized_execution.summary or {"total": 1, "counts": {finalized_execution.status: 1}},
        "result": finalized_execution.result,
        "item": _extract_result_item(finalized_execution.result),
        "items": finalized_execution.result.get("items", [])
        if isinstance(finalized_execution.result, dict)
        else [],
        "error": finalized_execution.error_text,
        "worker_id": finalized_execution.worker_id,
        **artifact_payload,
    }
    return payload


def dispatch_phase1_outbox_once(params: dict[str, Any]) -> dict[str, Any]:
    settings = _phase1_settings(params)
    store = _create_store(settings)
    outbox = store.claim_next_outbox()
    if outbox is None:
        return {
            "control_action": "outbox_once",
            "dispatcher_status": "idle",
            "processed_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "message": "No outbox message is ready to dispatch.",
            "summary": {"total": 0, "counts": {}},
            "item": {},
            "items": [],
        }
    channel_code = str(outbox.channel_code or "noop").strip() or "noop"
    try:
        payload = outbox.payload if isinstance(outbox.payload, dict) else {}
        message_text = str(payload.get("message_text", "") or _json_dumps(payload))
        if channel_code in {"noop", "disabled"}:
            sent = store.mark_outbox_sent(outbox_id=outbox.outbox_id)
        elif channel_code in {"console", "stdout"}:
            print(message_text)
            sent = store.mark_outbox_sent(outbox_id=outbox.outbox_id)
        elif channel_code in {"feishu_bot_api", "feishu_direct_api"}:
            transport_payload = _dispatch_via_feishu_bot_api(
                message_text=message_text,
                reply_target=outbox.reply_target,
            )
            sent = store.mark_outbox_sent(outbox_id=outbox.outbox_id)
            payload["transport"] = transport_payload
        elif channel_code in {"openclaw_message", "feishu_openclaw"}:
            transport_payload = _dispatch_via_openclaw_message(
                message_text=message_text,
                reply_target=outbox.reply_target,
            )
            sent = store.mark_outbox_sent(outbox_id=outbox.outbox_id)
            payload["transport"] = transport_payload
        else:
            raise RuntimeError(f"Unsupported outbox channel '{channel_code}'.")
        return {
            "control_action": "outbox_once",
            "dispatcher_status": "processed",
            "processed_count": 1,
            "success_count": 1,
            "failed_count": 0,
            "message": "Outbox dispatcher sent one notification.",
            "summary": {"total": 1, "counts": {"sent": 1}},
            "item": sent.to_dict(),
            "items": [sent.to_dict()],
            "outbox_id": sent.outbox_id,
            "request_id": sent.ref_id,
            "channel_code": sent.channel_code,
            "reply_target": sent.reply_target,
        }
    except Exception as exc:
        failed = store.mark_outbox_retry_or_failed(
            outbox_id=outbox.outbox_id,
            error_text=str(exc),
            retry_delay_seconds=float(settings["retry_delay_seconds"]),
        )
        final_status = "failed" if failed.status == "failed" else "retry_wait"
        return {
            "control_action": "outbox_once",
            "dispatcher_status": "processed",
            "processed_count": 1,
            "success_count": 0,
            "failed_count": 1 if final_status == "failed" else 0,
            "message": "Outbox dispatcher failed to send one notification.",
            "summary": {"total": 1, "counts": {final_status: 1}},
            "item": failed.to_dict(),
            "items": [failed.to_dict()],
            "outbox_id": failed.outbox_id,
            "request_id": failed.ref_id,
            "channel_code": failed.channel_code,
            "reply_target": failed.reply_target,
            "error": failed.last_error_text,
        }


def _run_loop(
    *,
    once_func: Any,
    action_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    stop_when_idle = _read_bool_param(params, "execution_control_stop_when_idle", False)
    max_iterations = max(_read_int_param(params, "execution_control_max_iterations", 0), 0)
    max_idle_cycles = max(_read_int_param(params, "execution_control_max_idle_cycles", 1), 1)
    poll_interval = max(_read_float_param(params, "execution_control_poll_interval_seconds", 1.0), 0.05)

    processed_items: list[dict[str, Any]] = []
    success_count = 0
    failed_count = 0
    idle_cycles = 0
    iterations = 0

    while True:
        payload = once_func(params)
        iterations += 1
        if payload.get("daemon_status") == "processed" or payload.get("dispatcher_status") == "processed":
            idle_cycles = 0
            processed_items.append(payload)
            success_count += int(payload.get("success_count", 0) or 0)
            failed_count += int(payload.get("failed_count", 0) or 0)
        else:
            idle_cycles += 1
        if max_iterations > 0 and iterations >= max_iterations:
            break
        if stop_when_idle and idle_cycles >= max_idle_cycles:
            break
        if not (payload.get("daemon_status") == "processed" or payload.get("dispatcher_status") == "processed"):
            time.sleep(poll_interval)

    last_item = processed_items[-1] if processed_items else {}
    summary_key = "dispatcher_status" if action_name == "outbox_loop" else "daemon_status"
    return {
        "control_action": action_name,
        summary_key: "completed" if processed_items else "idle",
        "processed_count": len(processed_items),
        "success_count": success_count,
        "failed_count": failed_count,
        "idle_cycles": idle_cycles,
        "iterations": iterations,
        "processed_items": processed_items,
        "last_item": last_item,
        "summary": {
            "total": len(processed_items),
            "counts": {
                "success": success_count,
                "failed": failed_count,
            },
        },
        "item": last_item.get("item", {}) if isinstance(last_item, dict) else {},
        "items": [item.get("item", {}) for item in processed_items if isinstance(item, dict)],
        "message": f"{action_name} exited after draining available work.",
    }


def run_phase1_executor_daemon(params: dict[str, Any]) -> dict[str, Any]:
    return _run_loop(
        once_func=execute_phase1_executor_once,
        action_name="executor_loop",
        params=params,
    )


def run_phase1_browser_runloop(params: dict[str, Any]) -> dict[str, Any]:
    return _run_loop(
        once_func=execute_phase1_browser_once,
        action_name="browser_loop",
        params=params,
    )


def run_phase1_outbox_dispatcher(params: dict[str, Any]) -> dict[str, Any]:
    return _run_loop(
        once_func=dispatch_phase1_outbox_once,
        action_name="outbox_loop",
        params=params,
    )


def _run_phase1_synchronously(params: dict[str, Any]) -> dict[str, Any]:
    submitted = submit_refresh_current_competitor_table(params)
    request_id = str(submitted["request_id"])
    executor_params = dict(params)
    executor_params["request_id"] = request_id
    first_executor = execute_phase1_executor_once(executor_params)
    if first_executor.get("request_status") == "waiting_children":
        browser_params = dict(params)
        browser_params["execution_control_stop_when_idle"] = True
        browser_params["execution_control_max_idle_cycles"] = 1
        run_phase1_browser_runloop(browser_params)
        execute_phase1_executor_once(executor_params)
    outbox_params = dict(params)
    outbox_params["execution_control_stop_when_idle"] = True
    outbox_params["execution_control_max_idle_cycles"] = 1
    run_phase1_outbox_dispatcher(outbox_params)
    result = get_refresh_current_competitor_table_status(
        {
            **params,
            "request_id": request_id,
        }
    )
    result["control_action"] = "run"
    result["message"] = "Phase 1 refresh task finished in synchronous compatibility mode."
    return result


def run_refresh_current_competitor_table(params: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("control_action", "run") or "run").strip().lower()
    if action == "submit":
        return submit_refresh_current_competitor_table(params)
    if action in {"status", "result"}:
        return get_refresh_current_competitor_table_status(params)
    if action == "executor_once":
        return execute_phase1_executor_once(params)
    if action == "executor_loop":
        return run_phase1_executor_daemon(params)
    if action == "browser_once":
        return execute_phase1_browser_once(params)
    if action == "browser_loop":
        return run_phase1_browser_runloop(params)
    if action == "outbox_once":
        return dispatch_phase1_outbox_once(params)
    if action == "outbox_loop":
        return run_phase1_outbox_dispatcher(params)
    return _run_phase1_synchronously(params)
