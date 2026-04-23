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
from typing import Any, Mapping

from automation_business_scaffold.config import get_execution_control_defaults
from automation_business_scaffold.infrastructure.artifacts.artifact_sync import (
    ArtifactFileSpec,
    build_artifact_payload as _build_synced_artifact_payload,
    collect_referenced_artifact_specs,
    create_store_from_settings,
    sync_artifact_specs,
)
from automation_business_scaffold.business.flows.feishu_competitor_flow import (
    run_fastmoss_keyword_candidate_discovery,
    run_feishu_pending_rows_scan,
    run_feishu_seed_row_insert,
    run_feishu_single_row_update,
)
from automation_business_scaffold.business.flows.resource_codes import build_browser_resource_code
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore
from automation_business_scaffold.infrastructure.facts.tk_fact_service import persist_product_fact_bundle
from automation_business_scaffold.infrastructure.facts.tk_fact_store import extract_fact_payloads
from automation_business_scaffold.business.flows.tiktok_feishu_sync_flow import run_tiktok_product_link_cleanup
from automation_business_scaffold.business.flows.influencer_pool_sync_flow import (
    run_sync_tk_influencer_pool as run_sync_tk_influencer_pool_sync,
)
from automation_business_scaffold.business.flows.tiktok_fastmoss_product_ingest_flow import (
    fetch_tiktok_product_via_browser,
    run_tiktok_fastmoss_product_ingest as run_tiktok_fastmoss_product_ingest_sync,
)
from automation_business_scaffold.business.flows.feishu_tk_selection_mapper import (
    DEFAULT_FEISHU_TK_SELECTION_TABLE_URL,
    FEISHU_TK_SELECTION_MAPPER_CODE,
    PRODUCT_STATUS_UNAVAILABLE,
    read_feishu_tk_selection_table_for_product,
    writeback_feishu_tk_selection_table,
)
from automation_business_scaffold.business.flows.tiktok_product_flow import (
    TikTokProductExtractionError,
    TikTokProductUnavailableError,
)
from automation_business_scaffold.models import ArtifactObjectRecord

REFRESH_TASK_CODE = "refresh_current_competitor_table"
KEYWORD_TASK_CODE = "search_keyword_competitor_products"
SYNC_TK_INFLUENCER_POOL_TASK_CODE = "sync_tk_influencer_pool"
TIKTOK_FASTMOSS_PRODUCT_INGEST_TASK_CODE = "tiktok_fastmoss_product_ingest"
TIKTOK_FASTMOSS_PRODUCT_INGEST_API_JOB_CODE = "tiktok_fastmoss_product_ingest"
FEISHU_TK_SELECTION_TABLE_READ_API_JOB_CODE = "feishu_tk_selection_table_read"
FEISHU_TK_SELECTION_TABLE_WRITEBACK_API_JOB_CODE = "feishu_tk_selection_table_writeback"
FEISHU_TK_SELECTION_SKIP_STATUSES = {"skipped_completed", "skipped_unavailable"}
TIKTOK_PRODUCT_BROWSER_FETCH_ITEM_CODE = "tiktok_product_browser_fetch"
TIKTOK_PRODUCT_BROWSER_FETCH_WORKFLOW_CODE = "tiktok_product_browser_fetch_v1"
SINGLE_ROW_UPDATE_ITEM_CODE = "feishu_single_row_update"
SINGLE_ROW_UPDATE_WORKFLOW_CODE = "feishu_single_row_update_v1"
KEYWORD_DISCOVERY_ITEM_CODE = "fastmoss_keyword_candidate_discovery"
KEYWORD_DISCOVERY_WORKFLOW_CODE = "fastmoss_keyword_candidate_discovery_v1"
BROWSER_SINGLE_ROW_STEP_ID = "execute_browser_single_row_update"
BROWSER_KEYWORD_DISCOVERY_STEP_ID = "execute_browser_keyword_candidate_discovery"
BROWSER_TIKTOK_PRODUCT_FETCH_STEP_ID = "execute_browser_tiktok_product_fetch"
SUPPORTED_BROWSER_ITEM_CODES = (
    KEYWORD_DISCOVERY_ITEM_CODE,
    SINGLE_ROW_UPDATE_ITEM_CODE,
    TIKTOK_PRODUCT_BROWSER_FETCH_ITEM_CODE,
)
KEYWORD_RESUME_DISCOVERY = "process_keyword_discovery"
KEYWORD_RESUME_DETAILS = "finalize_keyword_detail_updates"
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


def _create_store(settings: dict[str, Any]) -> RuntimeStore:
    return RuntimeStore(db_url=str(settings.get("db_url", "") or ""))


def _runtime_params_from_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "execution_control_db_url": settings.get("db_url", ""),
        "execution_control_artifact_root": settings.get("artifact_root", ""),
        "execution_control_artifact_bucket": settings.get("artifact_bucket", ""),
        "execution_control_artifact_store_provider": settings.get("artifact_store_provider", ""),
        "execution_control_artifact_object_prefix": settings.get("artifact_object_prefix", ""),
        "execution_control_minio_endpoint": settings.get("minio_endpoint", ""),
        "execution_control_minio_access_key": settings.get("minio_access_key", ""),
        "execution_control_minio_secret_key": settings.get("minio_secret_key", ""),
        "execution_control_minio_region": settings.get("minio_region", ""),
        "execution_control_minio_secure": settings.get("minio_secure", False),
        "execution_control_minio_create_bucket": settings.get("minio_create_bucket", False),
        "execution_control_sync_referenced_files": settings.get("sync_referenced_files", False),
    }


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
        store: RuntimeStore,
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


class _CallbackHeartbeat:
    def __init__(
        self,
        *,
        callback: Any,
        interval_seconds: float,
    ):
        self._callback = callback
        self._interval_seconds = max(interval_seconds, 0.1)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                self._callback()
            except Exception:
                return

    def __enter__(self) -> "_CallbackHeartbeat":
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


def _browser_step_id_for_execution(execution: Any) -> str:
    item_code = str(getattr(execution, "item_code", "") or "").strip()
    if item_code == KEYWORD_DISCOVERY_ITEM_CODE:
        return BROWSER_KEYWORD_DISCOVERY_STEP_ID
    if item_code == TIKTOK_PRODUCT_BROWSER_FETCH_ITEM_CODE:
        return BROWSER_TIKTOK_PRODUCT_FETCH_STEP_ID
    return BROWSER_SINGLE_ROW_STEP_ID


def _build_refresh_enqueue_items(
    request_payload: dict[str, Any],
    target_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    resource_code = build_browser_resource_code(request_payload)
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
                "dedupe_key": f"{SINGLE_ROW_UPDATE_ITEM_CODE}:{profile_ref}:{record_id}",
                "resource_code": resource_code,
                "max_attempts": 3,
                "payload": payload,
            }
        )
    return items


def _build_keyword_discovery_items(request_payload: dict[str, Any]) -> list[dict[str, Any]]:
    resource_code = build_browser_resource_code(request_payload)
    search_keyword = str(request_payload.get("search_keyword", "") or request_payload.get("keyword", "")).strip()
    sales_7d_threshold = str(request_payload.get("sales_7d_threshold", "") or "200").strip() or "200"
    business_key = f"{search_keyword}:{sales_7d_threshold}"
    payload = dict(request_payload)
    return [
        {
            "business_key": business_key,
            "dedupe_key": "",
            "resource_code": resource_code,
            "max_attempts": 3,
            "payload": payload,
        }
    ]


def _build_tiktok_product_browser_fetch_items(
    request_payload: dict[str, Any],
    *,
    product_url: str,
    product_id: str,
    request_id: str,
    fallback_reason: str,
) -> list[dict[str, Any]]:
    payload = dict(request_payload)
    payload["product_url"] = product_url
    if product_id:
        payload["product_id"] = product_id
    payload["profile_ref"] = _browser_fallback_profile_ref(payload)
    payload["tiktok_browser_fallback_reason"] = fallback_reason
    payload["trace_id"] = _first_non_empty_mapping(payload, "trace_id") or request_id
    resource_code = build_browser_resource_code(payload)
    business_key = product_id or product_url or request_id
    return [
        {
            "business_key": business_key,
            "dedupe_key": f"{TIKTOK_PRODUCT_BROWSER_FETCH_ITEM_CODE}:{request_id}",
            "resource_code": resource_code,
            "max_attempts": int(payload.get("browser_max_attempts", 3) or 3),
            "payload": payload,
        }
    ]


def _browser_fallback_profile_ref(params: Mapping[str, Any]) -> str:
    return _first_non_empty_mapping(
        params,
        "tiktok_browser_profile_ref",
        "browser_profile_ref",
        "profile_ref",
    ) or str(os.environ.get("BROWSER_PROFILE_REF") or "roxy-tiktok").strip() or "roxy-tiktok"


def _build_keyword_detail_enqueue_items(
    request_payload: dict[str, Any],
    seed_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    resource_code = build_browser_resource_code(request_payload)
    profile_ref = str(request_payload.get("profile_ref", "") or "main").strip() or "main"
    items: list[dict[str, Any]] = []
    for seed in seed_items:
        if not isinstance(seed, dict):
            continue
        record_id = str(seed.get("record_id", "") or "").strip()
        status = str(seed.get("status", "") or "").strip()
        if not record_id or status != "inserted":
            continue
        payload = dict(request_payload)
        payload["record_id"] = record_id
        if seed.get("normalized_url"):
            payload["source_url"] = seed.get("normalized_url")
        elif seed.get("source_url"):
            payload["source_url"] = seed.get("source_url")
        if seed.get("product_id"):
            payload["sku_id"] = seed.get("product_id")
        items.append(
            {
                "business_key": record_id,
                "dedupe_key": f"{SINGLE_ROW_UPDATE_ITEM_CODE}:{profile_ref}:{record_id}",
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


def _summarize_item_status_counts(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "") or "").strip() or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return {"total": len(items), "counts": counts}


def _build_refresh_live_summary(request: Any, executions: list[Any]) -> dict[str, Any]:
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


def _build_keyword_live_summary(request: Any, executions: list[Any]) -> dict[str, Any]:
    stage_cursor = request.stage_cursor if isinstance(request.stage_cursor, dict) else {}
    discovery_payload = stage_cursor.get("discovery", {})
    discovery_summary = discovery_payload.get("summary", {}) if isinstance(discovery_payload, dict) else {}
    seed_payload = stage_cursor.get("seed_insert", {})
    seed_summary = seed_payload.get("summary", {}) if isinstance(seed_payload, dict) else {}

    total = 0
    counts: dict[str, int] = {}
    if isinstance(discovery_summary, dict):
        try:
            total = max(total, int(discovery_summary.get("total", 0) or 0))
        except (TypeError, ValueError):
            pass
        for key, value in _summary_counts_only(discovery_summary).items():
            counts[key] = counts.get(key, 0) + int(value)
    if isinstance(seed_summary, dict):
        for key, value in _summary_counts_only(seed_summary).items():
            counts[key] = counts.get(key, 0) + int(value)
    detail_executions = [
        execution
        for execution in executions
        if str(getattr(execution, "item_code", "") or "") == SINGLE_ROW_UPDATE_ITEM_CODE
    ]
    detail_success_count = sum(1 for execution in detail_executions if str(execution.status or "") == "success")
    detail_failed_count = sum(1 for execution in detail_executions if str(execution.status or "") == "failed")
    detail_skipped_count = sum(1 for execution in detail_executions if str(execution.status or "") == "skipped")
    active_count = sum(
        1
        for execution in executions
        if str(getattr(execution, "status", "") or "") in {"pending", "running", "retry_wait"}
    )
    if detail_success_count > 0:
        counts["success"] = detail_success_count
    if detail_failed_count > 0:
        counts["failed"] = detail_failed_count
    if detail_skipped_count > 0:
        counts["skipped"] = detail_skipped_count
    if request.status in {"pending", "running", "waiting_children", "ready_for_summary"} and active_count > 0:
        counts["active"] = active_count
    if request.status == "pending" and not counts:
        counts["queued"] = 1
        total = max(total, 1)
    return {"total": total, "counts": counts}


def _build_live_summary(request: Any, executions: list[Any]) -> dict[str, Any]:
    task_code = str(getattr(request, "task_code", "") or "").strip()
    if task_code == KEYWORD_TASK_CODE:
        return _build_keyword_live_summary(request, executions)
    return _build_refresh_live_summary(request, executions)


def _build_refresh_result_payload(request: Any, executions: list[Any], outbox_records: list[Any]) -> dict[str, Any]:
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
    fact_payloads = extract_fact_payloads(item_results)
    return {
        "task_request": request.to_dict(),
        "cleanup": stage_cursor.get("cleanup", {}),
        "scan": stage_cursor.get("scan", {}),
        "enqueue": stage_cursor.get("enqueue", {}),
        "executions": [execution.to_dict() for execution in executions],
        "outbox": [record.to_dict() for record in outbox_records],
        "items": item_results,
        "failed_items": failed_items,
        **fact_payloads,
    }


def _build_keyword_result_payload(request: Any, executions: list[Any], outbox_records: list[Any]) -> dict[str, Any]:
    stage_cursor = request.stage_cursor if isinstance(request.stage_cursor, dict) else {}
    discovery_payload = stage_cursor.get("discovery", {})
    seed_payload = stage_cursor.get("seed_insert", {})
    enqueue_updates_payload = stage_cursor.get("enqueue_updates", {})
    discovery_execution = next(
        (
            execution.to_dict()
            for execution in executions
            if str(execution.item_code or "") == KEYWORD_DISCOVERY_ITEM_CODE
        ),
        {},
    )
    detail_executions = [
        execution
        for execution in executions
        if str(execution.item_code or "") == SINGLE_ROW_UPDATE_ITEM_CODE
    ]
    item_results: list[dict[str, Any]] = []
    failed_items: list[dict[str, Any]] = []
    for execution in detail_executions:
        item = _extract_result_item(execution.result if isinstance(execution.result, dict) else {})
        if not item:
            continue
        enriched_item = dict(item)
        enriched_item.setdefault("execution_id", execution.execution_id)
        enriched_item.setdefault("execution_status", execution.status)
        item_results.append(enriched_item)
        if execution.status == "failed":
            failed_items.append(enriched_item)
    fact_payloads = extract_fact_payloads(item_results)
    return {
        "task_request": request.to_dict(),
        "discovery": discovery_payload,
        "discovery_execution": discovery_execution,
        "seed_insert": seed_payload,
        "enqueue_updates": enqueue_updates_payload,
        "executions": [execution.to_dict() for execution in executions],
        "outbox": [record.to_dict() for record in outbox_records],
        "items": item_results,
        "failed_items": failed_items,
        **fact_payloads,
    }


def _build_result_payload(request: Any, executions: list[Any], outbox_records: list[Any]) -> dict[str, Any]:
    task_code = str(getattr(request, "task_code", "") or "").strip()
    if task_code == KEYWORD_TASK_CODE:
        return _build_keyword_result_payload(request, executions, outbox_records)
    return _build_refresh_result_payload(request, executions, outbox_records)


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _build_refresh_outbox_text(request: Any, summary: dict[str, Any]) -> str:
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


def _build_keyword_outbox_text(request: Any, summary: dict[str, Any], result: dict[str, Any]) -> str:
    payload = request.payload if isinstance(getattr(request, "payload", {}), dict) else {}
    search_keyword = str(payload.get("search_keyword", "") or "").strip()
    discovery_payload = result.get("discovery", {}) if isinstance(result, dict) else {}
    discovery_summary = discovery_payload.get("summary", {}) if isinstance(discovery_payload, dict) else {}
    discovery_execution = result.get("discovery_execution", {}) if isinstance(result, dict) else {}
    discovery_result = discovery_execution.get("result", {}) if isinstance(discovery_execution, dict) else {}
    seed_payload = result.get("seed_insert", {}) if isinstance(result, dict) else {}
    seed_summary = seed_payload.get("summary", {}) if isinstance(seed_payload, dict) else {}

    discovery_counts = _summary_counts_only(discovery_summary)
    seed_counts = _summary_counts_only(seed_summary)
    final_counts = _summary_counts_only(summary)

    pages_scanned = _safe_int(discovery_result.get("pages_scanned", 0) if isinstance(discovery_result, dict) else 0)
    rows_scanned = _safe_int(discovery_result.get("rows_scanned", 0) if isinstance(discovery_result, dict) else 0)
    candidate_new = _safe_int(discovery_counts.get("candidate_new", 0))
    skipped_existing = _safe_int(discovery_counts.get("skipped_existing", 0))
    inserted = _safe_int(seed_counts.get("inserted", 0))
    detail_success = _safe_int(final_counts.get("success", 0))
    detail_failed = _safe_int(final_counts.get("failed", 0))
    detail_skipped = _safe_int(final_counts.get("skipped", 0))

    parts = [f"关键词 {search_keyword or request.request_id} 搜索完成"]
    if pages_scanned > 0:
        parts.append(f"扫描 {pages_scanned} 页")
    if rows_scanned > 0:
        parts.append(f"读取 {rows_scanned} 行")
    if candidate_new > 0:
        parts.append(f"新候选 {candidate_new} 条")
    else:
        parts.append("命中 0 条候选")
    if skipped_existing > 0:
        parts.append(f"已存在 {skipped_existing} 条")
    if inserted > 0:
        parts.append(f"写入 {inserted} 条")
    else:
        parts.append("未写入新记录")

    detail_counts: list[str] = []
    if detail_success > 0:
        detail_counts.append(f"success={detail_success}")
    if detail_failed > 0:
        detail_counts.append(f"failed={detail_failed}")
    if detail_skipped > 0:
        detail_counts.append(f"skipped={detail_skipped}")
    if detail_counts:
        parts.append(f"详情补全 {' '.join(detail_counts)}")
    return "；".join(parts)


def _build_product_ingest_outbox_text(request: Any, summary: dict[str, Any], result: dict[str, Any]) -> str:
    product_id = str(result.get("product_id", "") or "").strip()
    media_upload = result.get("media_upload", {}) if isinstance(result.get("media_upload"), dict) else {}
    media_summary = media_upload.get("summary", {}) if isinstance(media_upload, dict) else {}
    media_counts = _summary_counts_only(media_summary) if isinstance(media_summary, dict) else {}
    fact_entity_count = 0
    persisted = result.get("persisted", {}) if isinstance(result.get("persisted"), dict) else {}
    persisted_summary = persisted.get("summary", {}) if isinstance(persisted, dict) else {}
    if isinstance(persisted_summary, dict):
        fact_entity_count = _safe_int(persisted_summary.get("fact_entity_count", 0))

    parts = [f"商品 {product_id or request.request_id} 事实采集完成"]
    uploaded_count = _safe_int(media_counts.get("uploaded", 0))
    failed_count = _safe_int(media_counts.get("failed", 0))
    if uploaded_count > 0:
        parts.append(f"上传图片 {uploaded_count} 张")
    if failed_count > 0:
        parts.append(f"图片失败 {failed_count} 张")
    if fact_entity_count > 0:
        parts.append(f"写入实体 {fact_entity_count} 条")
    return "；".join(parts)


def _build_influencer_pool_request_outbox_text(
    request: Any,
    summary: dict[str, Any],
    result: dict[str, Any],
) -> str:
    write_summary = result.get("write_summary", {}) if isinstance(result.get("write_summary"), dict) else {}
    counts = _summary_counts_only(summary)
    total = _safe_int(summary.get("total", 0))
    failed_count = _safe_int(
        write_summary.get(
            "failed_item_count",
            result.get("failed_item_count", counts.get("failed", 0)),
        )
    )
    created_count = _safe_int(write_summary.get("created_author_count", 0))
    updated_count = _safe_int(write_summary.get("updated_author_count", 0))
    skipped_count = _safe_int(write_summary.get("already_synced_author_count", 0))

    if bool(write_summary.get("hard_stopped")):
        status_label = "已中断，等待重试"
    elif failed_count > 0:
        status_label = "完成但有失败"
    else:
        status_label = "完成"

    parts = [f"达人池同步 {status_label}"]
    if total > 0:
        parts.append(f"来源记录 {total} 条")
    if created_count > 0:
        parts.append(f"新增达人 {created_count}")
    if updated_count > 0:
        parts.append(f"更新达人 {updated_count}")
    if skipped_count > 0:
        parts.append(f"跳过已同步 {skipped_count}")
    if failed_count > 0:
        parts.append(f"失败 {failed_count}")
    if len(parts) == 1:
        parts.append(f"request_id={request.request_id}")
    return "；".join(parts)


def _build_outbox_text(request: Any, summary: dict[str, Any], result: dict[str, Any]) -> str:
    task_code = str(getattr(request, "task_code", "") or "").strip()
    if task_code == KEYWORD_TASK_CODE:
        return _build_keyword_outbox_text(request, summary, result if isinstance(result, dict) else {})
    if task_code == SYNC_TK_INFLUENCER_POOL_TASK_CODE:
        return _build_influencer_pool_request_outbox_text(
            request,
            summary,
            result if isinstance(result, dict) else {},
        )
    if task_code == TIKTOK_FASTMOSS_PRODUCT_INGEST_TASK_CODE:
        return _build_product_ingest_outbox_text(request, summary, result if isinstance(result, dict) else {})
    return _build_refresh_outbox_text(request, summary)


def _build_failure_outbox_text(request: Any, error_text: str) -> str:
    task_code = str(getattr(request, "task_code", "") or "").strip()
    payload = request.payload if isinstance(getattr(request, "payload", {}), dict) else {}
    if task_code == KEYWORD_TASK_CODE:
        search_keyword = str(payload.get("search_keyword", "") or "").strip()
        prefix = f"关键词 {search_keyword} 搜索失败" if search_keyword else f"任务 {request.request_id} 执行失败"
        return f"{prefix}：{error_text}" if error_text else prefix
    if task_code == SYNC_TK_INFLUENCER_POOL_TASK_CODE:
        prefix = "达人池同步失败"
        return f"{prefix}：{error_text}" if error_text else prefix
    if task_code == TIKTOK_FASTMOSS_PRODUCT_INGEST_TASK_CODE:
        product_url = str(payload.get("product_url", "") or payload.get("url", "") or "").strip()
        prefix = f"商品 {product_url or request.request_id} 事实采集失败"
        return f"{prefix}：{error_text}" if error_text else prefix
    return f"任务 {request.request_id} 执行失败"


def _build_request_payload(
    *,
    store: RuntimeStore,
    request_id: str,
    control_action: str,
    message: str,
) -> dict[str, Any]:
    request = store.load_task_request(request_id=request_id)
    executions = store.list_task_executions(request_id=request_id)
    api_worker_jobs = store.list_api_worker_jobs_for_request(request_id=request_id)
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
        "api_worker_jobs": api_worker_jobs,
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
    store: RuntimeStore,
    settings: dict[str, Any],
    execution: Any,
    result_payload: dict[str, Any],
    error_text: str,
    stdout_path: Path,
) -> dict[str, Any]:
    artifact_root = Path(str(settings["artifact_root"])).expanduser()
    run_root = artifact_root / "runs" / execution.run_id
    step_id = _browser_step_id_for_execution(execution)
    artifacts_root = run_root / "artifacts" / step_id
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
            "step_id": step_id,
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
            "step_id": step_id,
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
            step_id=step_id,
            relative_name="run.json",
            path=run_file,
        ),
        ArtifactFileSpec(
            kind="steps_json",
            step_id=step_id,
            relative_name="steps.json",
            path=steps_file,
        ),
        ArtifactFileSpec(
            kind="signals_json",
            step_id=step_id,
            relative_name="signals.json",
            path=signals_file,
        ),
        ArtifactFileSpec(
            kind="stdout_log",
            step_id=step_id,
            relative_name="stdout.log",
            path=stdout_path,
        ),
        ArtifactFileSpec(
            kind="state_json",
            step_id=step_id,
            relative_name=f"artifacts/{step_id}/state.json",
            path=state_file,
        ),
    ]
    if bool(settings.get("sync_referenced_files", False)):
        specs.extend(
            collect_referenced_artifact_specs(
                result_payload=result_payload,
                step_id=step_id,
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
    store: RuntimeStore,
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
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        finished_at=time.time(),
    )
    store.create_notification_outbox(
        channel_code=request.source_channel_code or "noop",
        event_type="task_request.completed",
        ref_id=request.request_id,
        reply_target=request.reply_target,
        payload={
            "message_text": _build_failure_outbox_text(request, error_text),
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
    store: RuntimeStore,
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
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        finished_at=time.time(),
    )
    summary_outbox = store.create_notification_outbox(
        channel_code=final_request.source_channel_code or "noop",
        event_type="task_request.completed",
        ref_id=final_request.request_id,
        reply_target=final_request.reply_target,
        payload={
            "message_text": _build_outbox_text(final_request, provisional_summary, result),
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
        task_code=REFRESH_TASK_CODE,
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


def submit_search_keyword_competitor_products(params: dict[str, Any]) -> dict[str, Any]:
    settings = _phase1_settings(params)
    store = _create_store(settings)
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code=KEYWORD_TASK_CODE,
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
        message="Top-level keyword search task accepted.",
    )


def submit_sync_tk_influencer_pool(params: dict[str, Any]) -> dict[str, Any]:
    settings = _phase1_settings(params)
    store = _create_store(settings)
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code=SYNC_TK_INFLUENCER_POOL_TASK_CODE,
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
        message="Top-level influencer pool sync task accepted.",
    )


def submit_tiktok_fastmoss_product_ingest(params: dict[str, Any]) -> dict[str, Any]:
    settings = _phase1_settings(params)
    store = _create_store(settings)
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code=TIKTOK_FASTMOSS_PRODUCT_INGEST_TASK_CODE,
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
        message="TikTok and FastMoss product ingest task accepted.",
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


def get_search_keyword_competitor_products_status(params: dict[str, Any]) -> dict[str, Any]:
    settings = _phase1_settings(params)
    store = _create_store(settings)
    return _build_request_payload(
        store=store,
        request_id=str(params.get("request_id", "") or ""),
        control_action="status",
        message="Loaded top-level keyword search task status.",
    )


def get_sync_tk_influencer_pool_status(params: dict[str, Any]) -> dict[str, Any]:
    settings = _phase1_settings(params)
    store = _create_store(settings)
    return _build_request_payload(
        store=store,
        request_id=str(params.get("request_id", "") or ""),
        control_action="status",
        message="Loaded top-level influencer pool sync task status.",
    )


def get_tiktok_fastmoss_product_ingest_status(params: dict[str, Any]) -> dict[str, Any]:
    settings = _phase1_settings(params)
    store = _create_store(settings)
    return _build_request_payload(
        store=store,
        request_id=str(params.get("request_id", "") or ""),
        control_action="status",
        message="Loaded TikTok and FastMoss product ingest task status.",
    )


def _plan_refresh_request(
    *,
    store: RuntimeStore,
    claimed_request: Any,
) -> dict[str, Any]:
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
        item_code=SINGLE_ROW_UPDATE_ITEM_CODE,
        workflow_code=SINGLE_ROW_UPDATE_WORKFLOW_CODE,
        items=_build_refresh_enqueue_items(request_payload, target_rows),
    )
    stage_cursor = {
        "cleanup": cleanup_payload,
        "scan": scan_payload,
        "enqueue": enqueue_payload,
        "resume_action": "finalize_refresh_summary",
    }
    child_total_count = int(enqueue_payload.get("created_count", 0) or 0)
    if child_total_count > 0:
        store.update_task_request(
            request_id=claimed_request.request_id,
            status="waiting_children",
            current_stage="waiting_children",
            stage_cursor=stage_cursor,
            error_text="",
            worker_id="",
            lease_until=0.0,
            heartbeat_at=0.0,
        )
        return _build_request_payload(
            store=store,
            request_id=claimed_request.request_id,
            control_action="executor_once",
            message="Top-level executor planned cleanup/scan and queued browser leaf tasks.",
        )
    store.update_task_request(
        request_id=claimed_request.request_id,
        status="ready_for_summary",
        current_stage="ready_for_summary",
        stage_cursor=stage_cursor,
        error_text="",
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
    )
    return _finalize_request_summary(store=store, request_id=claimed_request.request_id)


def _plan_keyword_request(
    *,
    store: RuntimeStore,
    claimed_request: Any,
) -> dict[str, Any]:
    request_payload = dict(claimed_request.payload)
    store.update_task_request(
        request_id=claimed_request.request_id,
        current_stage="keyword_candidate_discovery",
        started_at=claimed_request.started_at or time.time(),
    )
    enqueue_payload = store.enqueue_task_executions(
        request_id=claimed_request.request_id,
        item_code=KEYWORD_DISCOVERY_ITEM_CODE,
        workflow_code=KEYWORD_DISCOVERY_WORKFLOW_CODE,
        items=_build_keyword_discovery_items(request_payload),
    )
    stage_cursor = {
        "discovery_enqueue": enqueue_payload,
        "resume_action": KEYWORD_RESUME_DISCOVERY,
    }
    if int(enqueue_payload.get("created_count", 0) or 0) > 0:
        store.update_task_request(
            request_id=claimed_request.request_id,
            status="waiting_children",
            current_stage="waiting_children",
            stage_cursor=stage_cursor,
            error_text="",
            worker_id="",
            lease_until=0.0,
            heartbeat_at=0.0,
        )
        return _build_request_payload(
            store=store,
            request_id=claimed_request.request_id,
            control_action="executor_once",
            message="Top-level executor queued one keyword discovery browser task.",
        )
    store.update_task_request(
        request_id=claimed_request.request_id,
        status="ready_for_summary",
        current_stage="ready_for_summary",
        stage_cursor=stage_cursor,
        error_text="",
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
    )
    return _finalize_request_summary(store=store, request_id=claimed_request.request_id)


def _resume_keyword_request(
    *,
    store: RuntimeStore,
    claimed_request: Any,
) -> dict[str, Any]:
    request_payload = dict(claimed_request.payload)
    stage_cursor = dict(claimed_request.stage_cursor or {})
    resume_action = str(stage_cursor.get("resume_action", "") or "")
    executions = store.list_task_executions(request_id=claimed_request.request_id)

    if resume_action == KEYWORD_RESUME_DISCOVERY:
        discovery_execution = next(
            (
                execution
                for execution in executions
                if str(execution.item_code or "") == KEYWORD_DISCOVERY_ITEM_CODE
            ),
            None,
        )
        if discovery_execution is None:
            return _fail_request(
                store=store,
                request_id=claimed_request.request_id,
                current_stage="keyword_discovery_missing",
                error_text="Keyword discovery execution result is missing.",
            )
        if discovery_execution.status == "failed":
            return _fail_request(
                store=store,
                request_id=claimed_request.request_id,
                current_stage="keyword_discovery_failed",
                error_text=discovery_execution.error_text or "Keyword discovery failed.",
            )

        discovery_payload = (
            dict(discovery_execution.result)
            if isinstance(discovery_execution.result, dict)
            else {}
        )
        target_items = discovery_payload.get("target_items", [])
        if not isinstance(target_items, list):
            target_items = []

        seed_items: list[dict[str, Any]] = []
        for candidate in target_items:
            if not isinstance(candidate, dict):
                continue
            seed_params = dict(request_payload)
            seed_params["sku_id"] = str(candidate.get("product_id", "") or "").strip()
            seed_params["search_keyword"] = str(
                request_payload.get("search_keyword", "") or request_payload.get("keyword", "")
            ).strip()
            candidate_url = str(
                candidate.get("normalized_product_url")
                or candidate.get("product_url")
                or candidate.get("normalized_url")
                or ""
            ).strip()
            if candidate_url:
                seed_params["product_url"] = candidate_url
            try:
                seed_payload = run_feishu_seed_row_insert(seed_params)
                seed_item = _extract_result_item(seed_payload if isinstance(seed_payload, dict) else {})
                if seed_item:
                    seed_items.append(seed_item)
            except Exception as exc:
                seed_items.append(
                    {
                        "record_id": "",
                        "product_id": str(candidate.get("product_id", "") or "").strip(),
                        "normalized_url": candidate_url,
                        "status": "insert_failed",
                        "error": str(exc),
                    }
                )

        seed_summary = _summarize_item_status_counts(seed_items)
        enqueue_updates_payload = store.enqueue_task_executions(
            request_id=claimed_request.request_id,
            item_code=SINGLE_ROW_UPDATE_ITEM_CODE,
            workflow_code=SINGLE_ROW_UPDATE_WORKFLOW_CODE,
            items=_build_keyword_detail_enqueue_items(request_payload, seed_items),
        )
        stage_cursor.update(
            {
                "discovery": discovery_payload,
                "seed_insert": {
                    "summary": seed_summary,
                    "items": seed_items,
                },
                "enqueue_updates": enqueue_updates_payload,
                "resume_action": KEYWORD_RESUME_DETAILS,
            }
        )
        if int(enqueue_updates_payload.get("created_count", 0) or 0) > 0:
            store.update_task_request(
                request_id=claimed_request.request_id,
                status="waiting_children",
                current_stage="waiting_children",
                stage_cursor=stage_cursor,
                error_text="",
                worker_id="",
                lease_until=0.0,
                heartbeat_at=0.0,
            )
            return _build_request_payload(
                store=store,
                request_id=claimed_request.request_id,
                control_action="executor_once",
                message="Top-level executor inserted keyword seed rows and queued detail updates.",
            )
        store.update_task_request(
            request_id=claimed_request.request_id,
            status="ready_for_summary",
            current_stage="ready_for_summary",
            stage_cursor=stage_cursor,
            error_text="",
            worker_id="",
            lease_until=0.0,
            heartbeat_at=0.0,
        )
        return _finalize_request_summary(store=store, request_id=claimed_request.request_id)

    if resume_action == KEYWORD_RESUME_DETAILS:
        return _finalize_request_summary(store=store, request_id=claimed_request.request_id)

    return _finalize_request_summary(store=store, request_id=claimed_request.request_id)


def _execute_tiktok_fastmoss_product_ingest_request(
    *,
    store: RuntimeStore,
    settings: dict[str, Any],
    claimed_request: Any,
) -> dict[str, Any]:
    request_payload = _tiktok_fastmoss_product_ingest_request_payload(
        settings=settings,
        claimed_request=claimed_request,
    )
    request_payload["notification_channel_code"] = ""
    request_payload["reply_target"] = ""
    if _uses_feishu_tk_selection_table(request_payload):
        return _dispatch_feishu_tk_selection_table_read_job(
            store=store,
            settings=settings,
            claimed_request=claimed_request,
            request_payload=request_payload,
        )
    return _dispatch_tiktok_fastmoss_product_ingest_api_job(
        store=store,
        settings=settings,
        claimed_request=claimed_request,
        request_payload=request_payload,
        stage_cursor={},
        waiting_stage="waiting_api_worker",
        message="Executor dispatched the TikTok/FastMoss product ingest API worker job.",
    )


def _tiktok_fastmoss_product_ingest_request_payload(
    *,
    settings: dict[str, Any],
    claimed_request: Any,
) -> dict[str, Any]:
    return {
        **dict(claimed_request.payload or {}),
        **_runtime_params_from_settings(settings),
        "request_id": claimed_request.request_id,
        "notification_channel_code": "",
        "reply_target": "",
    }


def _uses_feishu_tk_selection_table(params: Mapping[str, Any]) -> bool:
    if _read_bool_param(dict(params), "skip_feishu_tk_selection_table", False):
        return False
    if _read_bool_param(dict(params), "table_read_required", False):
        return True
    if _read_bool_param(dict(params), "feishu_tk_selection_enabled", False):
        return True
    if _first_non_empty_mapping(
        params,
        "tk_selection_table_url",
        "feishu_tk_selection_table_url",
    ):
        return True
    if _generic_table_url_is_tk_selection(params) and _first_non_empty_mapping(params, "table_url"):
        return True
    return False


def _feishu_tk_selection_table_url_from_params(params: Mapping[str, Any]) -> str:
    explicit_url = _first_non_empty_mapping(
        params,
        "tk_selection_table_url",
        "feishu_tk_selection_table_url",
    )
    if explicit_url:
        return explicit_url
    if _generic_table_url_is_tk_selection(params):
        return _first_non_empty_mapping(params, "table_url") or DEFAULT_FEISHU_TK_SELECTION_TABLE_URL
    return DEFAULT_FEISHU_TK_SELECTION_TABLE_URL


def _generic_table_url_is_tk_selection(params: Mapping[str, Any]) -> bool:
    mapper_code = str(params.get("field_mapper_code", "") or "").strip()
    if mapper_code == FEISHU_TK_SELECTION_MAPPER_CODE:
        return True
    table_kind = str(
        params.get("feishu_table_kind")
        or params.get("table_kind")
        or params.get("table_name")
        or ""
    ).strip()
    return table_kind in {"tk_selection", "TK选品收集"}


def _first_non_empty_mapping(params: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(params.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _is_feishu_tk_selection_skip_status(status: Any) -> bool:
    return str(status or "").strip() in FEISHU_TK_SELECTION_SKIP_STATUSES


def _dispatch_feishu_tk_selection_table_read_job(
    *,
    store: RuntimeStore,
    settings: dict[str, Any],
    claimed_request: Any,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    product_key = str(
        request_payload.get("product_url")
        or request_payload.get("source_url")
        or request_payload.get("url")
        or claimed_request.request_id
    ).strip()
    table_payload = {
        **request_payload,
        "tk_selection_table_url": _feishu_tk_selection_table_url_from_params(request_payload),
        "field_mapper_code": FEISHU_TK_SELECTION_MAPPER_CODE,
    }
    store.update_task_request(
        request_id=claimed_request.request_id,
        current_stage="dispatch_feishu_tk_selection_table_read",
        started_at=claimed_request.started_at or time.time(),
    )
    enqueue_payload = store.enqueue_api_worker_jobs(
        request_id=claimed_request.request_id,
        task_code=TIKTOK_FASTMOSS_PRODUCT_INGEST_TASK_CODE,
        job_code=FEISHU_TK_SELECTION_TABLE_READ_API_JOB_CODE,
        jobs=[
            {
                "business_key": product_key,
                "dedupe_key": (
                    f"{FEISHU_TK_SELECTION_TABLE_READ_API_JOB_CODE}:{claimed_request.request_id}"
                ),
                "payload": table_payload,
                "max_attempts": int(table_payload.get("max_attempts", 3) or 3),
            }
        ],
    )
    result_payload = _build_tiktok_fastmoss_product_ingest_result_from_api_jobs(
        store=store,
        request_id=claimed_request.request_id,
        dispatch_payload={"feishu_tk_selection_table_read": enqueue_payload},
    )
    job_summary = result_payload.get("api_worker_job_summary", {})
    if not isinstance(job_summary, Mapping):
        job_summary = {}
    store.update_task_request(
        request_id=claimed_request.request_id,
        status="waiting_children",
        current_stage="waiting_feishu_tk_selection_table_read",
        summary=result_payload["summary"],
        result=result_payload,
        stage_cursor={
            "feishu_tk_selection": {
                "table_url": table_payload["tk_selection_table_url"],
                "mapper_code": FEISHU_TK_SELECTION_MAPPER_CODE,
            },
            "table_read_dispatch": enqueue_payload,
        },
        error_text="",
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        **_api_worker_job_child_counts(job_summary),
    )
    return _build_request_payload(
        store=store,
        request_id=claimed_request.request_id,
        control_action="executor_once",
        message="Executor dispatched the Feishu TK selection table read API worker job.",
    )


def _dispatch_tiktok_fastmoss_product_ingest_api_job(
    *,
    store: RuntimeStore,
    settings: dict[str, Any],
    claimed_request: Any,
    request_payload: dict[str, Any],
    stage_cursor: Mapping[str, Any],
    waiting_stage: str,
    message: str,
) -> dict[str, Any]:
    store.update_task_request(
        request_id=claimed_request.request_id,
        current_stage="dispatch_tiktok_fastmoss_product_ingest_api_job",
        started_at=claimed_request.started_at or time.time(),
    )
    product_id = str(
        request_payload.get("sku_id")
        or request_payload.get("product_id")
        or request_payload.get("product_url")
        or claimed_request.request_id
    )
    dedupe_suffix = str(request_payload.get("api_job_dedupe_key_suffix", "") or "").strip()
    dedupe_key = f"{TIKTOK_FASTMOSS_PRODUCT_INGEST_API_JOB_CODE}:{claimed_request.request_id}"
    if dedupe_suffix:
        dedupe_key = f"{dedupe_key}:{dedupe_suffix}"
    enqueue_payload = store.enqueue_api_worker_jobs(
        request_id=claimed_request.request_id,
        task_code=TIKTOK_FASTMOSS_PRODUCT_INGEST_TASK_CODE,
        job_code=TIKTOK_FASTMOSS_PRODUCT_INGEST_API_JOB_CODE,
        jobs=[
            {
                "business_key": product_id,
                "dedupe_key": dedupe_key,
                "payload": request_payload,
                "max_attempts": int(request_payload.get("max_attempts", 3) or 3),
            }
        ],
    )
    result_payload = _build_tiktok_fastmoss_product_ingest_result_from_api_jobs(
        store=store,
        request_id=claimed_request.request_id,
        dispatch_payload={"tiktok_fastmoss_product_ingest": enqueue_payload},
    )
    job_summary = result_payload.get("api_worker_job_summary", {})
    if not isinstance(job_summary, Mapping):
        job_summary = {}
    active_count = int(job_summary.get("active_count") or 0)
    total_count = int(job_summary.get("total") or 0)
    next_status = "ready_for_summary" if total_count == 0 or active_count == 0 else "waiting_children"
    next_stage = "ready_for_summary" if next_status == "ready_for_summary" else waiting_stage
    updated_stage_cursor = dict(stage_cursor)
    updated_stage_cursor["product_ingest_dispatch"] = enqueue_payload
    store.update_task_request(
        request_id=claimed_request.request_id,
        status=next_status,
        current_stage=next_stage,
        summary=result_payload["summary"],
        result=result_payload,
        stage_cursor=updated_stage_cursor,
        error_text="",
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        **_api_worker_job_child_counts(job_summary),
    )
    if next_status == "ready_for_summary":
        return _finalize_tiktok_fastmoss_product_ingest_request(
            store=store,
            request_id=claimed_request.request_id,
        )
    return _build_request_payload(
        store=store,
        request_id=claimed_request.request_id,
        control_action="executor_once",
        message=message,
    )


def _dispatch_tiktok_product_browser_fallback_job(
    *,
    store: RuntimeStore,
    settings: dict[str, Any],
    claimed_request: Any,
) -> dict[str, Any]:
    request_payload = _tiktok_fastmoss_product_ingest_request_payload(
        settings=settings,
        claimed_request=claimed_request,
    )
    fallback_result = _latest_tiktok_browser_fallback_required_result(
        store=store,
        request_id=claimed_request.request_id,
    )
    fallback_item = _as_result_item(fallback_result)
    if not fallback_result:
        raise RuntimeError("TikTok browser fallback was requested, but no fallback-required product job result exists.")
    product_url = (
        _first_non_empty_mapping(fallback_item, "product_url", "normalized_url", "source_url", "url")
        or _first_non_empty_mapping(request_payload, "product_url", "normalized_url", "source_url", "url")
    )
    if not product_url:
        raise RuntimeError("TikTok browser fallback requires product_url.")
    product_id = (
        _first_non_empty_mapping(fallback_item, "product_id")
        or _first_non_empty_mapping(request_payload, "product_id", "sku_id")
    )
    fallback_reason = _first_non_empty_mapping(fallback_item, "error", "reason") or _first_non_empty_mapping(
        fallback_result,
        "error",
        "reason",
    )
    enqueue_payload = store.enqueue_task_executions(
        request_id=claimed_request.request_id,
        item_code=TIKTOK_PRODUCT_BROWSER_FETCH_ITEM_CODE,
        workflow_code=TIKTOK_PRODUCT_BROWSER_FETCH_WORKFLOW_CODE,
        items=_build_tiktok_product_browser_fetch_items(
            request_payload,
            product_url=product_url,
            product_id=product_id,
            request_id=claimed_request.request_id,
            fallback_reason=fallback_reason,
        ),
    )
    stage_cursor = {
        **dict(claimed_request.stage_cursor or {}),
        "tiktok_browser_fallback_required": fallback_result,
        "tiktok_browser_fallback_dispatch": enqueue_payload,
    }
    store.update_task_request(
        request_id=claimed_request.request_id,
        status="waiting_children",
        current_stage="waiting_tiktok_product_browser_fetch",
        summary={"total": 1, "counts": {"waiting_tiktok_browser_fallback": 1}},
        stage_cursor=stage_cursor,
        error_text="",
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
    )
    return _build_request_payload(
        store=store,
        request_id=claimed_request.request_id,
        control_action="executor_once",
        message="Executor dispatched the TikTok product browser fallback job.",
    )


def _api_worker_job_child_counts(job_summary: Mapping[str, Any]) -> dict[str, int]:
    total = int(job_summary.get("total") or 0)
    active = int(job_summary.get("active_count") or 0)
    success = int(job_summary.get("success_count") or 0)
    failed = int(job_summary.get("failed_count") or 0)
    terminal = max(total - active, 0)
    return {
        "child_total_count": total,
        "child_terminal_count": terminal,
        "child_success_count": success,
        "child_failed_count": failed,
        "child_skipped_count": 0,
    }


def _build_tiktok_fastmoss_product_ingest_result_from_api_jobs(
    *,
    store: RuntimeStore,
    request_id: str,
    dispatch_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    job_summary = store.summarize_api_worker_jobs_for_request(request_id=request_id)
    jobs = store.list_api_worker_jobs_for_request(request_id=request_id)
    executions = store.list_task_executions(request_id=request_id)
    browser_fetch_executions = [
        execution.to_dict()
        for execution in executions
        if str(execution.item_code or "") == TIKTOK_PRODUCT_BROWSER_FETCH_ITEM_CODE
    ]
    success_jobs = [job for job in jobs if str(job.get("status", "") or "") == "success"]
    failed_jobs = [job for job in jobs if str(job.get("status", "") or "") == "failed"]
    success_jobs_by_code: dict[str, list[dict[str, Any]]] = {}
    for job in success_jobs:
        success_jobs_by_code.setdefault(str(job.get("job_code", "") or ""), []).append(job)
    result: dict[str, Any] = {}
    product_success_jobs = success_jobs_by_code.get(TIKTOK_FASTMOSS_PRODUCT_INGEST_API_JOB_CODE, [])
    table_read_success_jobs = success_jobs_by_code.get(FEISHU_TK_SELECTION_TABLE_READ_API_JOB_CODE, [])
    writeback_success_jobs = success_jobs_by_code.get(FEISHU_TK_SELECTION_TABLE_WRITEBACK_API_JOB_CODE, [])
    if product_success_jobs:
        job_result = product_success_jobs[-1].get("result", {})
        if isinstance(job_result, Mapping):
            result.update(dict(job_result))
    elif table_read_success_jobs:
        job_result = table_read_success_jobs[-1].get("result", {})
        if isinstance(job_result, Mapping):
            result.update(dict(job_result))
    if table_read_success_jobs:
        table_read_result = table_read_success_jobs[-1].get("result", {})
        if isinstance(table_read_result, Mapping):
            result["feishu_tk_selection_table_read"] = dict(table_read_result)
    if writeback_success_jobs:
        writeback_result = writeback_success_jobs[-1].get("result", {})
        if isinstance(writeback_result, Mapping):
            result["feishu_tk_selection_table_writeback"] = dict(writeback_result)
    if not result:
        result.update(
            {
                "status": "pending" if int(job_summary.get("active_count") or 0) > 0 else "failed",
                "summary": {
                    "total": int(job_summary.get("total") or 0),
                    "counts": dict(job_summary.get("counts") or {}),
                },
                "item": {
                    "request_id": request_id,
                    "status": "waiting_api_worker"
                    if int(job_summary.get("active_count") or 0) > 0
                    else "failed",
                },
                "items": [],
            }
        )
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else None
    if summary is None:
        summary = {
            "total": int(job_summary.get("total") or 0),
            "counts": dict(job_summary.get("counts") or {}),
        }
        result["summary"] = summary
    result["api_worker_job_summary"] = job_summary
    result["feishu_tk_selection_table_read_job_summary"] = store.summarize_api_worker_jobs_for_request(
        request_id=request_id,
        job_code=FEISHU_TK_SELECTION_TABLE_READ_API_JOB_CODE,
    )
    result["product_ingest_job_summary"] = store.summarize_api_worker_jobs_for_request(
        request_id=request_id,
        job_code=TIKTOK_FASTMOSS_PRODUCT_INGEST_API_JOB_CODE,
    )
    result["feishu_tk_selection_table_writeback_job_summary"] = store.summarize_api_worker_jobs_for_request(
        request_id=request_id,
        job_code=FEISHU_TK_SELECTION_TABLE_WRITEBACK_API_JOB_CODE,
    )
    result["api_worker_jobs"] = jobs
    if browser_fetch_executions:
        result["tiktok_browser_fallback_executions"] = browser_fetch_executions
    result["failed_items"] = failed_jobs
    result["failed_item_count"] = len(failed_jobs)
    result["queue_mode"] = "api_worker"
    if dispatch_payload:
        result["dispatch"] = dict(dispatch_payload)
    return result


def _mark_tiktok_fastmoss_product_ingest_ready_if_done(
    *,
    store: RuntimeStore,
    request_id: str,
) -> dict[str, Any]:
    if not request_id:
        return {"updated": False, "request_id": "", "reason": "missing_request_id"}
    request = store.load_task_request(request_id=request_id)
    if request.task_code != TIKTOK_FASTMOSS_PRODUCT_INGEST_TASK_CODE:
        return {"updated": False, "request_id": request_id, "reason": "not_product_ingest"}
    if request.status not in {"waiting_children", "running"}:
        return {"updated": False, "request_id": request_id, "reason": f"status_{request.status}"}
    result_payload = _build_tiktok_fastmoss_product_ingest_result_from_api_jobs(
        store=store,
        request_id=request_id,
    )
    job_summary = result_payload.get("api_worker_job_summary", {})
    if not isinstance(job_summary, Mapping):
        job_summary = {}
    if int(job_summary.get("total") or 0) > 0 and int(job_summary.get("active_count") or 0) > 0:
        store.update_task_request(
            request_id=request_id,
            status="waiting_children",
            current_stage=request.current_stage or "waiting_api_worker",
            summary=result_payload["summary"],
            result=result_payload,
            error_text="",
            worker_id="",
            lease_until=0.0,
            heartbeat_at=0.0,
            **_api_worker_job_child_counts(job_summary),
        )
        return {
            "updated": False,
            "request_id": request_id,
            "reason": "active_jobs_remain",
            "api_worker_job_summary": job_summary,
        }
    if request.current_stage in {"waiting_api_worker", "waiting_tiktok_fastmoss_product_ingest"}:
        fallback_result = _latest_tiktok_browser_fallback_required_result(
            store=store,
            request_id=request_id,
        )
        if fallback_result and not _latest_successful_tiktok_browser_fetch_result(
            store=store,
            request_id=request_id,
        ):
            store.update_task_request(
                request_id=request_id,
                status="pending",
                current_stage="dispatch_tiktok_product_browser_fallback",
                summary=result_payload["summary"],
                result=result_payload,
                stage_cursor={
                    **dict(request.stage_cursor or {}),
                    "tiktok_browser_fallback_required": fallback_result,
                },
                error_text="",
                worker_id="",
                lease_until=0.0,
                heartbeat_at=0.0,
                **_api_worker_job_child_counts(job_summary),
            )
            return {
                "updated": True,
                "request_id": request_id,
                "reason": "tiktok_browser_fallback_required",
                "next_stage": "dispatch_tiktok_product_browser_fallback",
                "api_worker_job_summary": job_summary,
            }
    if int(job_summary.get("failed_count") or 0) > 0:
        store.update_task_request(
            request_id=request_id,
            status="ready_for_summary",
            current_stage="ready_for_summary",
            summary=result_payload["summary"],
            result=result_payload,
            error_text="",
            worker_id="",
            lease_until=0.0,
            heartbeat_at=0.0,
            **_api_worker_job_child_counts(job_summary),
        )
        return {
            "updated": True,
            "request_id": request_id,
            "reason": "failed_job_terminal",
            "api_worker_job_summary": job_summary,
        }
    if request.current_stage == "waiting_feishu_tk_selection_table_read":
        table_read_result = _latest_successful_api_worker_job_result(
            store=store,
            request_id=request_id,
            job_code=FEISHU_TK_SELECTION_TABLE_READ_API_JOB_CODE,
        )
        table_read_item = table_read_result.get("item", {}) if isinstance(table_read_result, dict) else {}
        table_status = str(table_read_item.get("status") or table_read_result.get("status") or "").strip()
        if _is_feishu_tk_selection_skip_status(table_status):
            store.update_task_request(
                request_id=request_id,
                status="ready_for_summary",
                current_stage="ready_for_summary",
                summary=result_payload["summary"],
                result=result_payload,
                error_text="",
                worker_id="",
                lease_until=0.0,
                heartbeat_at=0.0,
                **_api_worker_job_child_counts(job_summary),
            )
            return {
                "updated": True,
                "request_id": request_id,
                "reason": f"table_read_{table_status}",
                "api_worker_job_summary": job_summary,
            }
        store.update_task_request(
            request_id=request_id,
            status="pending",
            current_stage="dispatch_tiktok_fastmoss_product_ingest_api_job",
            summary=result_payload["summary"],
            result=result_payload,
            stage_cursor={
                **dict(request.stage_cursor or {}),
                "table_read_result": table_read_result,
            },
            error_text="",
            worker_id="",
            lease_until=0.0,
            heartbeat_at=0.0,
            **_api_worker_job_child_counts(job_summary),
        )
        return {
            "updated": True,
            "request_id": request_id,
            "reason": "table_read_needs_ingest",
            "next_stage": "dispatch_tiktok_fastmoss_product_ingest_api_job",
            "api_worker_job_summary": job_summary,
        }
    if request.current_stage == "waiting_tiktok_fastmoss_product_ingest":
        store.update_task_request(
            request_id=request_id,
            status="pending",
            current_stage="dispatch_feishu_tk_selection_table_writeback",
            summary=result_payload["summary"],
            result=result_payload,
            error_text="",
            worker_id="",
            lease_until=0.0,
            heartbeat_at=0.0,
            **_api_worker_job_child_counts(job_summary),
        )
        return {
            "updated": True,
            "request_id": request_id,
            "reason": "product_ingest_completed_needs_writeback",
            "next_stage": "dispatch_feishu_tk_selection_table_writeback",
            "api_worker_job_summary": job_summary,
        }
    store.update_task_request(
        request_id=request_id,
        status="ready_for_summary",
        current_stage="ready_for_summary",
        summary=result_payload["summary"],
        result=result_payload,
        error_text="",
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        **_api_worker_job_child_counts(job_summary),
    )
    return {
        "updated": True,
        "request_id": request_id,
        "reason": "all_jobs_terminal",
        "api_worker_job_summary": job_summary,
    }


def _latest_successful_api_worker_job_result(
    *,
    store: RuntimeStore,
    request_id: str,
    job_code: str,
) -> dict[str, Any]:
    jobs = store.list_api_worker_jobs_for_request(request_id=request_id, job_code=job_code)
    for job in reversed(jobs):
        if str(job.get("status", "") or "") != "success":
            continue
        result = job.get("result", {})
        return dict(result) if isinstance(result, Mapping) else {}
    return {}


def _latest_successful_api_worker_job(
    *,
    store: RuntimeStore,
    request_id: str,
    job_code: str,
) -> dict[str, Any]:
    jobs = store.list_api_worker_jobs_for_request(request_id=request_id, job_code=job_code)
    for job in reversed(jobs):
        if str(job.get("status", "") or "") == "success":
            return dict(job)
    return {}


def _latest_tiktok_browser_fallback_required_result(
    *,
    store: RuntimeStore,
    request_id: str,
) -> dict[str, Any]:
    jobs = store.list_api_worker_jobs_for_request(
        request_id=request_id,
        job_code=TIKTOK_FASTMOSS_PRODUCT_INGEST_API_JOB_CODE,
    )
    for job in reversed(jobs):
        if str(job.get("status", "") or "") != "success":
            continue
        result = job.get("result", {})
        if not isinstance(result, Mapping):
            continue
        status = str(result.get("status") or _as_result_item(result).get("status") or "").strip()
        if status == "tiktok_browser_fallback_required":
            return dict(result)
    return {}


def _latest_successful_tiktok_browser_fetch_result(
    *,
    store: RuntimeStore,
    request_id: str,
) -> dict[str, Any]:
    executions = store.list_task_executions(request_id=request_id)
    for execution in reversed(executions):
        if str(execution.item_code or "") != TIKTOK_PRODUCT_BROWSER_FETCH_ITEM_CODE:
            continue
        if str(execution.status or "") != "success":
            continue
        result = execution.result if isinstance(execution.result, dict) else {}
        return dict(result) if isinstance(result, Mapping) else {}
    return {}


def _as_result_item(payload: Mapping[str, Any]) -> dict[str, Any]:
    item = payload.get("item")
    return dict(item) if isinstance(item, Mapping) else {}


def _dispatch_tiktok_fastmoss_product_ingest_after_table_read(
    *,
    store: RuntimeStore,
    settings: dict[str, Any],
    claimed_request: Any,
) -> dict[str, Any]:
    table_read_result = _latest_successful_api_worker_job_result(
        store=store,
        request_id=claimed_request.request_id,
        job_code=FEISHU_TK_SELECTION_TABLE_READ_API_JOB_CODE,
    )
    table_read_item = table_read_result.get("item", {}) if isinstance(table_read_result.get("item"), Mapping) else {}
    if not table_read_result:
        raise RuntimeError("Feishu TK selection table-read result is required before product ingest dispatch.")
    if _is_feishu_tk_selection_skip_status(table_read_item.get("status") or table_read_result.get("status")):
        store.update_task_request(
            request_id=claimed_request.request_id,
            status="ready_for_summary",
            current_stage="ready_for_summary",
            worker_id="",
            lease_until=0.0,
            heartbeat_at=0.0,
        )
        return _finalize_tiktok_fastmoss_product_ingest_request(
            store=store,
            request_id=claimed_request.request_id,
        )

    request_payload = _tiktok_fastmoss_product_ingest_request_payload(
        settings=settings,
        claimed_request=claimed_request,
    )
    product_payload = {
        **request_payload,
        "product_url": _first_non_empty_mapping(table_read_item, "product_url", "normalized_url")
        or _first_non_empty_mapping(request_payload, "product_url", "source_url", "url"),
        "product_id": _first_non_empty_mapping(table_read_item, "product_id")
        or _first_non_empty_mapping(request_payload, "product_id", "sku_id"),
        "source_record_id": _first_non_empty_mapping(table_read_item, "source_record_id", "record_id"),
        "field_mapper_code": FEISHU_TK_SELECTION_MAPPER_CODE,
        "tk_selection_table_url": _first_non_empty_mapping(table_read_item, "table_url")
        or _feishu_tk_selection_table_url_from_params(request_payload),
        "table_read_result": table_read_result,
    }
    product_payload.setdefault("fastmoss_visualization_enabled", True)
    product_payload = _attach_tiktok_browser_fallback_payload(
        store=store,
        request_id=claimed_request.request_id,
        product_payload=product_payload,
    )
    return _dispatch_tiktok_fastmoss_product_ingest_api_job(
        store=store,
        settings=settings,
        claimed_request=claimed_request,
        request_payload=product_payload,
        stage_cursor={
            **dict(claimed_request.stage_cursor or {}),
            "table_read_result": table_read_result,
        },
        waiting_stage="waiting_tiktok_fastmoss_product_ingest",
        message="Executor dispatched the TikTok/FastMoss product ingest API worker job after Feishu table-read.",
    )


def _attach_tiktok_browser_fallback_payload(
    *,
    store: RuntimeStore,
    request_id: str,
    product_payload: dict[str, Any],
) -> dict[str, Any]:
    browser_result = _latest_successful_tiktok_browser_fetch_result(
        store=store,
        request_id=request_id,
    )
    if not browser_result:
        return product_payload
    payload = dict(product_payload)
    payload["tiktok_payload"] = browser_result
    payload["tiktok_browser_fallback_result"] = browser_result
    payload["tiktok_fetch_source"] = "browser"
    payload["api_job_dedupe_key_suffix"] = "after-browser-fallback"
    product_id = _first_non_empty_mapping(browser_result, "product_id") or _first_non_empty_mapping(
        _as_result_item(browser_result),
        "product_id",
    )
    if product_id:
        payload["product_id"] = product_id
    return payload


def _dispatch_feishu_tk_selection_table_writeback_job(
    *,
    store: RuntimeStore,
    settings: dict[str, Any],
    claimed_request: Any,
) -> dict[str, Any]:
    request_payload = _tiktok_fastmoss_product_ingest_request_payload(
        settings=settings,
        claimed_request=claimed_request,
    )
    table_read_result = _latest_successful_api_worker_job_result(
        store=store,
        request_id=claimed_request.request_id,
        job_code=FEISHU_TK_SELECTION_TABLE_READ_API_JOB_CODE,
    )
    product_ingest_result = _latest_successful_api_worker_job_result(
        store=store,
        request_id=claimed_request.request_id,
        job_code=TIKTOK_FASTMOSS_PRODUCT_INGEST_API_JOB_CODE,
    )
    if not table_read_result:
        raise RuntimeError("Feishu TK selection table-read result is required before writeback dispatch.")
    if not product_ingest_result:
        raise RuntimeError("Product ingest result is required before Feishu TK selection writeback dispatch.")
    table_read_item = table_read_result.get("item", {}) if isinstance(table_read_result.get("item"), Mapping) else {}
    record_id = _first_non_empty_mapping(table_read_item, "source_record_id", "record_id")
    product_key = _first_non_empty_mapping(product_ingest_result, "product_id") or record_id or claimed_request.request_id
    writeback_payload = {
        **request_payload,
        "source_record_id": record_id,
        "field_mapper_code": FEISHU_TK_SELECTION_MAPPER_CODE,
        "tk_selection_table_url": _first_non_empty_mapping(table_read_item, "table_url")
        or _feishu_tk_selection_table_url_from_params(request_payload),
        "table_read_result": table_read_result,
        "product_ingest_result": product_ingest_result,
    }
    store.update_task_request(
        request_id=claimed_request.request_id,
        current_stage="dispatch_feishu_tk_selection_table_writeback",
        started_at=claimed_request.started_at or time.time(),
    )
    enqueue_payload = store.enqueue_api_worker_jobs(
        request_id=claimed_request.request_id,
        task_code=TIKTOK_FASTMOSS_PRODUCT_INGEST_TASK_CODE,
        job_code=FEISHU_TK_SELECTION_TABLE_WRITEBACK_API_JOB_CODE,
        jobs=[
            {
                "business_key": product_key,
                "dedupe_key": (
                    f"{FEISHU_TK_SELECTION_TABLE_WRITEBACK_API_JOB_CODE}:{claimed_request.request_id}"
                ),
                "payload": writeback_payload,
                "max_attempts": int(writeback_payload.get("max_attempts", 3) or 3),
            }
        ],
    )
    result_payload = _build_tiktok_fastmoss_product_ingest_result_from_api_jobs(
        store=store,
        request_id=claimed_request.request_id,
        dispatch_payload={"feishu_tk_selection_table_writeback": enqueue_payload},
    )
    job_summary = result_payload.get("api_worker_job_summary", {})
    if not isinstance(job_summary, Mapping):
        job_summary = {}
    store.update_task_request(
        request_id=claimed_request.request_id,
        status="waiting_children",
        current_stage="waiting_feishu_tk_selection_table_writeback",
        summary=result_payload["summary"],
        result=result_payload,
        stage_cursor={
            **dict(claimed_request.stage_cursor or {}),
            "writeback_dispatch": enqueue_payload,
        },
        error_text="",
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        **_api_worker_job_child_counts(job_summary),
    )
    return _build_request_payload(
        store=store,
        request_id=claimed_request.request_id,
        control_action="executor_once",
        message="Executor dispatched the Feishu TK selection table writeback API worker job.",
    )


def _finalize_tiktok_fastmoss_product_ingest_request(
    *,
    store: RuntimeStore,
    request_id: str,
) -> dict[str, Any]:
    result_payload = _build_tiktok_fastmoss_product_ingest_result_from_api_jobs(
        store=store,
        request_id=request_id,
    )
    summary = (
        dict(result_payload.get("summary"))
        if isinstance(result_payload.get("summary"), dict)
        else {"total": 1, "counts": {"success": 1}}
    )
    job_summary = result_payload.get("api_worker_job_summary", {})
    if not isinstance(job_summary, Mapping):
        job_summary = {}
    failed_items = result_payload.get("failed_items", [])
    if not isinstance(failed_items, list):
        failed_items = []
    failed_count = int(job_summary.get("failed_count") or 0)
    final_status = "failed" if failed_count > 0 else "success"
    error_text = ""
    if final_status == "failed" and failed_items:
        first_failed = failed_items[0] if isinstance(failed_items[0], Mapping) else {}
        error_text = str(first_failed.get("error_text", "") or "TikTok/FastMoss product ingest failed.")
    final_request = store.update_task_request(
        request_id=request_id,
        status=final_status,
        current_stage="completed" if final_status == "success" else "failed",
        summary=summary,
        result=result_payload,
        error_text=error_text,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        finished_at=time.time(),
        **_api_worker_job_child_counts(job_summary),
    )
    message_text = (
        _build_product_ingest_outbox_text(final_request, summary, result_payload)
        if final_status == "success"
        else _build_failure_outbox_text(final_request, error_text)
    )
    final_request = store.update_task_request(
        request_id=request_id,
        result=result_payload,
    )
    summary_outbox = store.create_notification_outbox(
        channel_code=final_request.source_channel_code or "noop",
        event_type="task_request.completed",
        ref_id=final_request.request_id,
        reply_target=final_request.reply_target,
        payload={
            "message_text": message_text,
            "request_id": final_request.request_id,
            "task_code": final_request.task_code,
            "summary": summary,
            "result": result_payload,
        },
        dedupe_key=f"task_request.completed:{final_request.request_id}",
    )
    result_payload["outbox"] = [summary_outbox.to_dict()]
    store.update_task_request(request_id=request_id, result=result_payload)
    return _build_request_payload(
        store=store,
        request_id=request_id,
        control_action="executor_once",
        message="Executor finalized the TikTok/FastMoss product ingest request.",
    )


def _build_sync_tk_influencer_pool_result_from_jobs(
    *,
    store: RuntimeStore,
    request_id: str,
    dispatch_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    product_summary = store.summarize_influencer_pool_product_jobs_for_request(request_id=request_id)
    product_jobs = store.list_influencer_pool_product_jobs_for_request(request_id=request_id)
    counts = dict(product_summary.get("counts") or {})
    failed_items = [
        dict(job)
        for job in product_jobs
        if str(job.get("status", "") or "") in {"hard_failed", "hard_stopped"}
    ]
    summary = {
        "total": int(product_summary.get("total") or 0),
        "counts": counts,
    }
    write_summary = {
        "product_job_count": int(product_summary.get("total") or 0),
        "product_job_success_count": int(product_summary.get("success_count") or 0),
        "product_job_failed_count": int(product_summary.get("failed_count") or 0),
        "matched_author_count": int(product_summary.get("matched_author_count") or 0),
        "queued_author_job_count": int(product_summary.get("queued_author_job_count") or 0),
        "created_author_count": 0,
        "updated_author_count": 0,
        "already_synced_author_count": 0,
        "failed_item_count": len(failed_items),
        "hard_stopped": any(str(item.get("status", "") or "") == "hard_stopped" for item in failed_items),
    }
    result = {
        "status": "success",
        "message": "Influencer pool API worker jobs reached terminal state.",
        "summary": summary,
        "write_summary": write_summary,
        "product_job_summary": product_summary,
        "items": [dict(job) for job in product_jobs],
        "failed_items": failed_items,
        "failed_item_count": len(failed_items),
        "queue_mode": "api_worker",
    }
    if dispatch_payload:
        result["dispatch"] = dict(dispatch_payload)
    return result


def _sync_tk_influencer_pool_child_counts(product_summary: Mapping[str, Any]) -> dict[str, int]:
    total = int(product_summary.get("total") or 0)
    active = int(product_summary.get("active_count") or 0)
    success = int(product_summary.get("success_count") or 0)
    failed = int(product_summary.get("failed_count") or 0)
    terminal = max(total - active, 0)
    return {
        "child_total_count": total,
        "child_terminal_count": terminal,
        "child_success_count": success,
        "child_failed_count": failed,
        "child_skipped_count": 0,
    }


def _mark_sync_tk_influencer_pool_ready_if_done(
    *,
    store: RuntimeStore,
    request_id: str,
) -> dict[str, Any]:
    if not request_id:
        return {"updated": False, "request_id": "", "reason": "missing_request_id"}
    request = store.load_task_request(request_id=request_id)
    if request.task_code != SYNC_TK_INFLUENCER_POOL_TASK_CODE:
        return {"updated": False, "request_id": request_id, "reason": "not_sync_tk_influencer_pool"}
    if request.status not in {"waiting_children", "running"}:
        return {"updated": False, "request_id": request_id, "reason": f"status_{request.status}"}
    product_summary = store.summarize_influencer_pool_product_jobs_for_request(request_id=request_id)
    if int(product_summary.get("total") or 0) > 0 and int(product_summary.get("active_count") or 0) > 0:
        result_payload = _build_sync_tk_influencer_pool_result_from_jobs(store=store, request_id=request_id)
        store.update_task_request(
            request_id=request_id,
            status="waiting_children",
            current_stage="waiting_children",
            summary=result_payload["summary"],
            result=result_payload,
            error_text="",
            worker_id="",
            lease_until=0.0,
            heartbeat_at=0.0,
            **_sync_tk_influencer_pool_child_counts(product_summary),
        )
        return {
            "updated": False,
            "request_id": request_id,
            "reason": "active_jobs_remain",
            "product_job_summary": product_summary,
        }
    result_payload = _build_sync_tk_influencer_pool_result_from_jobs(store=store, request_id=request_id)
    store.update_task_request(
        request_id=request_id,
        status="ready_for_summary",
        current_stage="ready_for_summary",
        summary=result_payload["summary"],
        result=result_payload,
        error_text="",
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        **_sync_tk_influencer_pool_child_counts(product_summary),
    )
    return {
        "updated": True,
        "request_id": request_id,
        "reason": "all_jobs_terminal",
        "product_job_summary": product_summary,
    }


def _finalize_sync_tk_influencer_pool_request(
    *,
    store: RuntimeStore,
    request_id: str,
) -> dict[str, Any]:
    existing_request = store.load_task_request(request_id=request_id)
    existing_result = existing_request.result if isinstance(existing_request.result, dict) else {}
    dispatch_payload = existing_result.get("dispatch") if isinstance(existing_result.get("dispatch"), Mapping) else None
    result_payload = _build_sync_tk_influencer_pool_result_from_jobs(
        store=store,
        request_id=request_id,
        dispatch_payload=dispatch_payload,
    )
    summary = dict(result_payload["summary"])
    product_summary = result_payload.get("product_job_summary", {})
    final_request = store.update_task_request(
        request_id=request_id,
        status="success",
        current_stage="completed",
        summary=summary,
        result=result_payload,
        error_text="",
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        finished_at=time.time(),
        **_sync_tk_influencer_pool_child_counts(product_summary if isinstance(product_summary, Mapping) else {}),
    )
    summary_outbox = store.create_notification_outbox(
        channel_code=final_request.source_channel_code or "noop",
        event_type="task_request.completed",
        ref_id=final_request.request_id,
        reply_target=final_request.reply_target,
        payload={
            "message_text": _build_influencer_pool_request_outbox_text(final_request, summary, result_payload),
            "request_id": final_request.request_id,
            "task_code": final_request.task_code,
            "summary": summary,
            "result": result_payload,
        },
        dedupe_key=f"task_request.completed:{final_request.request_id}",
    )
    result_payload["outbox"] = [summary_outbox.to_dict()]
    store.update_task_request(request_id=request_id, result=result_payload)
    return _build_request_payload(
        store=store,
        request_id=request_id,
        control_action="executor_once",
        message="Executor finalized the influencer pool sync request.",
    )


def _execute_sync_tk_influencer_pool_request(
    *,
    store: RuntimeStore,
    settings: dict[str, Any],
    claimed_request: Any,
) -> dict[str, Any]:
    request_payload = {
        **dict(claimed_request.payload or {}),
        **_runtime_params_from_settings(settings),
        "request_id": claimed_request.request_id,
    }
    # The task_request wrapper owns final user notification; suppress the legacy
    # direct-run summary outbox to avoid duplicate OpenClaw messages.
    request_payload["notification_channel_code"] = ""
    request_payload["reply_target"] = ""
    request_payload["queue_mode"] = "dispatch_only"
    store.update_task_request(
        request_id=claimed_request.request_id,
        current_stage="dispatch_influencer_pool_product_jobs",
        started_at=claimed_request.started_at or time.time(),
    )
    result_payload = run_sync_tk_influencer_pool_sync(request_payload)
    queue_result = _build_sync_tk_influencer_pool_result_from_jobs(
        store=store,
        request_id=claimed_request.request_id,
        dispatch_payload=result_payload,
    )
    product_summary = queue_result.get("product_job_summary", {})
    if not isinstance(product_summary, Mapping):
        product_summary = {}
    active_count = int(product_summary.get("active_count") or 0)
    total_count = int(product_summary.get("total") or 0)
    next_status = "ready_for_summary" if total_count == 0 or active_count == 0 else "waiting_children"
    next_stage = "ready_for_summary" if next_status == "ready_for_summary" else "waiting_children"
    store.update_task_request(
        request_id=claimed_request.request_id,
        status=next_status,
        current_stage=next_stage,
        summary=queue_result["summary"],
        result=queue_result,
        stage_cursor={"dispatch": result_payload},
        error_text="",
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        **_sync_tk_influencer_pool_child_counts(product_summary),
    )
    if next_status == "ready_for_summary":
        return _finalize_sync_tk_influencer_pool_request(
            store=store,
            request_id=claimed_request.request_id,
        )
    return _build_request_payload(
        store=store,
        request_id=claimed_request.request_id,
        control_action="executor_once",
        message="Executor dispatched influencer pool product jobs for API worker processing.",
    )


def execute_executor_once(params: dict[str, Any]) -> dict[str, Any]:
    settings = _phase1_settings(params)
    store = _create_store(settings)
    claimed_request = store.claim_next_task_request(
        worker_id=str(settings["worker_id"]),
        lease_seconds=float(settings["lease_seconds"]),
    )
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
            with _CallbackHeartbeat(
                callback=lambda: store.heartbeat_task_request(
                    request_id=claimed_request.request_id,
                    lease_seconds=float(settings["lease_seconds"]),
                ),
                interval_seconds=min(
                    float(settings["heartbeat_interval_seconds"]),
                    float(settings["lease_seconds"]),
                ),
            ):
                task_code = str(claimed_request.task_code or "")
                if task_code == KEYWORD_TASK_CODE:
                    payload = _plan_keyword_request(store=store, claimed_request=claimed_request)
                elif task_code == REFRESH_TASK_CODE:
                    payload = _plan_refresh_request(store=store, claimed_request=claimed_request)
                elif task_code == SYNC_TK_INFLUENCER_POOL_TASK_CODE:
                    payload = _execute_sync_tk_influencer_pool_request(
                        store=store,
                        settings=settings,
                        claimed_request=claimed_request,
                    )
                elif task_code == TIKTOK_FASTMOSS_PRODUCT_INGEST_TASK_CODE:
                    payload = _execute_tiktok_fastmoss_product_ingest_request(
                        store=store,
                        settings=settings,
                        claimed_request=claimed_request,
                    )
                else:
                    payload = _fail_request(
                        store=store,
                        request_id=claimed_request.request_id,
                        current_stage="unsupported_task_code",
                        error_text=f"Unsupported task_code '{task_code}'.",
                    )
            processed_failed = str(payload.get("request_status", "") or "").strip().lower() == "failed"
            payload.update(
                {
                    "daemon_status": "processed",
                    "processed_count": 1,
                    "success_count": 0 if processed_failed else 1,
                    "failed_count": 1 if processed_failed else 0,
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

    if current_stage in {
        "dispatch_tiktok_fastmoss_product_ingest_api_job",
        "dispatch_tiktok_product_browser_fallback",
        "dispatch_feishu_tk_selection_table_writeback",
    }:
        try:
            with _CallbackHeartbeat(
                callback=lambda: store.heartbeat_task_request(
                    request_id=claimed_request.request_id,
                    lease_seconds=float(settings["lease_seconds"]),
                ),
                interval_seconds=min(
                    float(settings["heartbeat_interval_seconds"]),
                    float(settings["lease_seconds"]),
                ),
            ):
                task_code = str(claimed_request.task_code or "")
                if task_code != TIKTOK_FASTMOSS_PRODUCT_INGEST_TASK_CODE:
                    payload = _fail_request(
                        store=store,
                        request_id=claimed_request.request_id,
                        current_stage="unsupported_task_code",
                        error_text=f"Unsupported task_code '{task_code}' at {current_stage}.",
                    )
                elif current_stage == "dispatch_tiktok_product_browser_fallback":
                    payload = _dispatch_tiktok_product_browser_fallback_job(
                        store=store,
                        settings=settings,
                        claimed_request=claimed_request,
                    )
                elif current_stage == "dispatch_tiktok_fastmoss_product_ingest_api_job":
                    table_read_result = _latest_successful_api_worker_job_result(
                        store=store,
                        request_id=claimed_request.request_id,
                        job_code=FEISHU_TK_SELECTION_TABLE_READ_API_JOB_CODE,
                    )
                    if table_read_result:
                        payload = _dispatch_tiktok_fastmoss_product_ingest_after_table_read(
                            store=store,
                            settings=settings,
                            claimed_request=claimed_request,
                        )
                    else:
                        request_payload = _tiktok_fastmoss_product_ingest_request_payload(
                            settings=settings,
                            claimed_request=claimed_request,
                        )
                        request_payload = _attach_tiktok_browser_fallback_payload(
                            store=store,
                            request_id=claimed_request.request_id,
                            product_payload=request_payload,
                        )
                        payload = _dispatch_tiktok_fastmoss_product_ingest_api_job(
                            store=store,
                            settings=settings,
                            claimed_request=claimed_request,
                            request_payload=request_payload,
                            stage_cursor=dict(claimed_request.stage_cursor or {}),
                            waiting_stage="waiting_api_worker",
                            message="Executor dispatched the TikTok/FastMoss product ingest API worker job.",
                        )
                else:
                    payload = _dispatch_feishu_tk_selection_table_writeback_job(
                        store=store,
                        settings=settings,
                        claimed_request=claimed_request,
                    )
            processed_failed = str(payload.get("request_status", "") or "").strip().lower() == "failed"
            payload.update(
                {
                    "daemon_status": "processed",
                    "processed_count": 1,
                    "success_count": 0 if processed_failed else 1,
                    "failed_count": 1 if processed_failed else 0,
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
            with _CallbackHeartbeat(
                callback=lambda: store.heartbeat_task_request(
                    request_id=claimed_request.request_id,
                    lease_seconds=float(settings["lease_seconds"]),
                ),
                interval_seconds=min(
                    float(settings["heartbeat_interval_seconds"]),
                    float(settings["lease_seconds"]),
                ),
            ):
                task_code = str(claimed_request.task_code or "")
                if task_code == KEYWORD_TASK_CODE:
                    payload = _resume_keyword_request(store=store, claimed_request=claimed_request)
                elif task_code == REFRESH_TASK_CODE:
                    payload = _finalize_request_summary(store=store, request_id=claimed_request.request_id)
                elif task_code == SYNC_TK_INFLUENCER_POOL_TASK_CODE:
                    payload = _finalize_sync_tk_influencer_pool_request(
                        store=store,
                        request_id=claimed_request.request_id,
                    )
                elif task_code == TIKTOK_FASTMOSS_PRODUCT_INGEST_TASK_CODE:
                    payload = _finalize_tiktok_fastmoss_product_ingest_request(
                        store=store,
                        request_id=claimed_request.request_id,
                    )
                else:
                    payload = _fail_request(
                        store=store,
                        request_id=claimed_request.request_id,
                        current_stage="unsupported_task_code",
                        error_text=f"Unsupported task_code '{task_code}' at ready_for_summary.",
                    )
            processed_failed = str(payload.get("request_status", "") or "").strip().lower() == "failed"
            payload.update(
                {
                    "daemon_status": "processed",
                    "processed_count": 1,
                    "success_count": 0 if processed_failed else 1,
                    "failed_count": 1 if processed_failed else 0,
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
    store: RuntimeStore,
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
                    f"request_id={execution.request_id} run_id={run_id} item_code={execution.item_code} status=running"
                )
                with _LeaseHeartbeat(
                    store=store,
                    execution_id=execution.execution_id,
                    lease_seconds=float(settings["lease_seconds"]),
                    interval_seconds=float(settings["heartbeat_interval_seconds"]),
                ):
                    if str(execution.item_code or "") == KEYWORD_DISCOVERY_ITEM_CODE:
                        result_payload = run_fastmoss_keyword_candidate_discovery(dict(execution.payload))
                    elif str(execution.item_code or "") == SINGLE_ROW_UPDATE_ITEM_CODE:
                        result_payload = run_feishu_single_row_update(dict(execution.payload))
                    elif str(execution.item_code or "") == TIKTOK_PRODUCT_BROWSER_FETCH_ITEM_CODE:
                        result_payload = fetch_tiktok_product_via_browser(dict(execution.payload))
                    else:
                        raise RuntimeError(f"Unsupported browser item_code '{execution.item_code}'.")
                terminal_status = _classify_execution_result(result_payload)
                if terminal_status == "success":
                    try:
                        if str(execution.item_code or "") == SINGLE_ROW_UPDATE_ITEM_CODE:
                            persist_product_fact_bundle(
                                store=store,
                                execution=execution,
                                result_payload=result_payload,
                                extract_result_item=_extract_result_item,
                            )
                    except Exception as entity_exc:
                        item = _extract_result_item(result_payload)
                        if item:
                            item["entity_error"] = str(entity_exc)
                            result_payload["item"] = item
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
                    f"run_id={run_id} item_code={execution.item_code} status={finalized_execution.status}"
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


def _mark_tiktok_fastmoss_product_ingest_after_browser_fallback(
    *,
    store: RuntimeStore,
    execution: Any,
) -> dict[str, Any]:
    if str(execution.item_code or "") != TIKTOK_PRODUCT_BROWSER_FETCH_ITEM_CODE:
        return {"updated": False, "request_id": str(execution.request_id or ""), "reason": "not_tiktok_browser_fetch"}
    request_id = str(execution.request_id or "")
    if not request_id:
        return {"updated": False, "request_id": "", "reason": "missing_request_id"}
    request = store.load_task_request(request_id=request_id)
    if str(request.task_code or "") != TIKTOK_FASTMOSS_PRODUCT_INGEST_TASK_CODE:
        return {"updated": False, "request_id": request_id, "reason": "not_product_ingest"}

    execution_status = str(execution.status or "")
    if execution_status == "success":
        result_payload = dict(execution.result) if isinstance(execution.result, dict) else {}
        store.update_task_request(
            request_id=request_id,
            status="pending",
            current_stage="dispatch_tiktok_fastmoss_product_ingest_api_job",
            stage_cursor={
                **dict(request.stage_cursor or {}),
                "tiktok_browser_fallback_result": result_payload,
                "tiktok_browser_fallback_execution_id": str(execution.execution_id or ""),
            },
            error_text="",
            worker_id="",
            lease_until=0.0,
            heartbeat_at=0.0,
        )
        return {
            "updated": True,
            "request_id": request_id,
            "reason": "tiktok_browser_fallback_completed",
            "next_stage": "dispatch_tiktok_fastmoss_product_ingest_api_job",
            "execution_id": str(execution.execution_id or ""),
        }

    if execution_status == "failed":
        error_text = str(execution.error_text or "TikTok product browser fallback failed.")
        failed_payload = _fail_request(
            store=store,
            request_id=request_id,
            current_stage="tiktok_browser_fallback_failed",
            error_text=error_text,
        )
        return {
            "updated": True,
            "request_id": request_id,
            "reason": "tiktok_browser_fallback_failed",
            "execution_id": str(execution.execution_id or ""),
            "request_status": failed_payload.get("request_status", "failed"),
        }

    return {
        "updated": False,
        "request_id": request_id,
        "reason": f"browser_execution_{execution_status}",
        "execution_id": str(execution.execution_id or ""),
    }


def execute_phase1_browser_once(params: dict[str, Any]) -> dict[str, Any]:
    settings = _phase1_settings(params)
    store = _create_store(settings)
    execution_id = str(params.get("execution_id", "") or "").strip()
    if execution_id:
        execution = store.claim_browser_execution(
            execution_id=execution_id,
            worker_id=str(settings["worker_id"]),
            lease_seconds=float(settings["lease_seconds"]),
        )
    else:
        execution = store.claim_next_browser_execution(
            worker_id=str(settings["worker_id"]),
            lease_seconds=float(settings["lease_seconds"]),
            request_id=str(params.get("request_id", "") or ""),
            item_codes=SUPPORTED_BROWSER_ITEM_CODES,
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
    parent_update = _mark_tiktok_fastmoss_product_ingest_after_browser_fallback(
        store=store,
        execution=finalized_execution,
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
        "parent_updates": [parent_update] if parent_update.get("updated") else [],
        **artifact_payload,
    }
    return payload


def dispatch_phase1_outbox_once(params: dict[str, Any]) -> dict[str, Any]:
    settings = _phase1_settings(params)
    store = _create_store(settings)
    outbox = store.claim_next_outbox(
        worker_id=str(settings["worker_id"]),
        lease_seconds=float(settings["lease_seconds"]),
    )
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
        with _CallbackHeartbeat(
            callback=lambda: store.heartbeat_outbox(
                outbox_id=outbox.outbox_id,
                lease_seconds=float(settings["lease_seconds"]),
            ),
            interval_seconds=min(
                float(settings["heartbeat_interval_seconds"]),
                float(settings["lease_seconds"]),
            ),
        ):
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


def _should_writeback_unavailable_product_status(
    *,
    exc: Exception,
    job_payload: Mapping[str, Any],
) -> bool:
    if not isinstance(exc, TikTokProductUnavailableError):
        return False
    if str(job_payload.get("field_mapper_code", "") or "").strip() == FEISHU_TK_SELECTION_MAPPER_CODE:
        return True
    if _first_non_empty_mapping(job_payload, "source_record_id", "tk_selection_table_url", "feishu_tk_selection_table_url"):
        return True
    return isinstance(job_payload.get("table_read_result"), Mapping)


def _should_dispatch_tiktok_browser_fallback(
    *,
    exc: Exception,
    job_payload: Mapping[str, Any],
) -> bool:
    if not isinstance(exc, TikTokProductExtractionError):
        return False
    if isinstance(exc, TikTokProductUnavailableError):
        return False
    if not _read_bool_param(dict(job_payload), "tiktok_browser_fallback_enabled", True):
        return False
    if _job_payload_has_tiktok_product_payload(job_payload):
        return False
    return bool(
        _first_non_empty_mapping(job_payload, "product_url", "source_url", "url", "normalized_url")
        or _first_non_empty_mapping(job_payload, "product_id", "sku_id")
    )


def _job_payload_has_tiktok_product_payload(job_payload: Mapping[str, Any]) -> bool:
    for key in ("tiktok_payload", "tiktok_product_payload", "tiktok_browser_fallback_result"):
        payload = job_payload.get(key)
        if isinstance(payload, Mapping) and isinstance(payload.get("product"), Mapping):
            return True
    return False


def _build_tiktok_browser_fallback_required_worker_result(
    *,
    worker_params: Mapping[str, Any],
    job_payload: Mapping[str, Any],
    error: Exception,
) -> dict[str, Any]:
    product_url = (
        _first_non_empty_mapping(job_payload, "product_url", "normalized_url", "source_url", "url")
        or _first_non_empty_mapping(worker_params, "product_url", "normalized_url", "source_url", "url")
    )
    product_id = (
        _first_non_empty_mapping(job_payload, "product_id", "sku_id")
        or _first_non_empty_mapping(worker_params, "product_id", "sku_id")
    )
    source_record_id = _first_non_empty_mapping(job_payload, "source_record_id")
    item = {
        "status": "tiktok_browser_fallback_required",
        "reason": "tiktok_request_extraction_failed",
        "error": str(error),
        "product_id": product_id,
        "product_url": product_url,
        "normalized_url": product_url,
        "source_record_id": source_record_id,
        "record_id": source_record_id,
        "fetch_source": "request",
        "next_fetch_source": "browser",
    }
    return {
        "summary": {"total": 1, "counts": {"tiktok_browser_fallback_required": 1}},
        "item": item,
        "items": [item],
        "status": "tiktok_browser_fallback_required",
        "reason": "tiktok_request_extraction_failed",
        "error_type": type(error).__name__,
        "error": str(error),
        "product_id": product_id,
        "product_url": product_url,
        "normalized_url": product_url,
        "source_record_id": source_record_id,
    }


def _build_unavailable_product_ingest_worker_result(
    *,
    worker_params: Mapping[str, Any],
    job_payload: Mapping[str, Any],
    error: Exception,
) -> dict[str, Any]:
    table_read_result = job_payload.get("table_read_result", {})
    if not isinstance(table_read_result, Mapping):
        table_read_result = {}
    table_read_item = table_read_result.get("item", {})
    if not isinstance(table_read_item, Mapping):
        table_read_item = {}
    product_url = (
        _first_non_empty_mapping(job_payload, "product_url", "normalized_url", "source_url", "url")
        or _first_non_empty_mapping(worker_params, "product_url", "normalized_url", "source_url", "url")
        or _first_non_empty_mapping(table_read_item, "product_url", "normalized_url")
    )
    product_id = (
        _first_non_empty_mapping(job_payload, "product_id", "sku_id")
        or _first_non_empty_mapping(worker_params, "product_id", "sku_id")
        or _first_non_empty_mapping(table_read_item, "product_id")
    )
    source_record_id = (
        _first_non_empty_mapping(job_payload, "source_record_id")
        or _first_non_empty_mapping(table_read_item, "source_record_id", "record_id")
    )
    item = {
        "status": "product_unavailable",
        "product_status": PRODUCT_STATUS_UNAVAILABLE,
        "reason": "tiktok_product_unavailable",
        "error": str(error),
        "product_id": product_id,
        "product_url": product_url,
        "normalized_url": product_url,
        "source_record_id": source_record_id,
        "record_id": source_record_id,
    }
    return {
        "summary": {"total": 1, "counts": {"product_unavailable": 1}},
        "item": item,
        "items": [item],
        "status": "product_unavailable",
        "product_status": PRODUCT_STATUS_UNAVAILABLE,
        "product_id": product_id,
        "product_url": product_url,
        "normalized_url": product_url,
        "source_record_id": source_record_id,
        "error_type": type(error).__name__,
        "error": str(error),
    }


def _run_tiktok_fastmoss_product_ingest_api_worker_job(
    *,
    store: RuntimeStore,
    settings: dict[str, Any],
    params: dict[str, Any],
    job: Mapping[str, Any],
) -> dict[str, Any]:
    job_id = str(job.get("job_id", "") or "")
    request_id = str(job.get("request_id", "") or "")
    run_id = str(job.get("run_id", "") or f"api-worker-{job_id}")
    result_payload: dict[str, Any] = {}
    job_payload: dict[str, Any] = {}
    worker_params: dict[str, Any] = {}
    try:
        job_payload = job.get("payload", {}) if isinstance(job.get("payload"), dict) else {}
        worker_params = {
            **params,
            **_runtime_params_from_settings(settings),
            **dict(job_payload),
            "request_id": request_id,
            "notification_channel_code": "",
            "reply_target": "",
        }
        with _CallbackHeartbeat(
            callback=lambda: store.heartbeat_api_worker_job(
                job_id=job_id,
                lease_seconds=float(settings["lease_seconds"]),
            ),
            interval_seconds=min(
                float(settings["heartbeat_interval_seconds"]),
                float(settings["lease_seconds"]),
            ),
        ):
            result_payload = run_tiktok_fastmoss_product_ingest_sync(worker_params)
        finalized_job = store.mark_api_worker_job_success(
            job_id=job_id,
            run_id=run_id,
            summary=result_payload.get("summary", {}) if isinstance(result_payload, dict) else {},
            result=result_payload if isinstance(result_payload, dict) else {},
        )
        parent_update = _mark_tiktok_fastmoss_product_ingest_ready_if_done(
            store=store,
            request_id=request_id,
        )
        return {
            "control_action": "api_worker_once",
            "daemon_status": "processed",
            "processed_count": 1,
            "success_count": 1,
            "failed_count": 0,
            "message": "API worker processed one TikTok/FastMoss product ingest job.",
            "request_id": request_id,
            "api_worker_job_id": job_id,
            "api_worker_job": finalized_job,
            "worker_result": result_payload,
            "parent_updates": [parent_update],
            "summary": finalized_job.get("summary", {"total": 1, "counts": {"success": 1}}),
            "item": finalized_job,
            "items": [finalized_job],
        }
    except Exception as exc:
        if _should_writeback_unavailable_product_status(exc=exc, job_payload=job_payload):
            result_payload = _build_unavailable_product_ingest_worker_result(
                worker_params=worker_params or job_payload,
                job_payload=job_payload,
                error=exc,
            )
            finalized_job = store.mark_api_worker_job_success(
                job_id=job_id,
                run_id=run_id,
                summary=result_payload["summary"],
                result=result_payload,
            )
            parent_update = _mark_tiktok_fastmoss_product_ingest_ready_if_done(
                store=store,
                request_id=request_id,
            )
            return {
                "control_action": "api_worker_once",
                "daemon_status": "processed",
                "processed_count": 1,
                "success_count": 1,
                "failed_count": 0,
                "message": "API worker marked one unavailable TikTok product for Feishu writeback.",
                "request_id": request_id,
                "api_worker_job_id": job_id,
                "api_worker_job": finalized_job,
                "worker_result": result_payload,
                "parent_updates": [parent_update],
                "summary": finalized_job.get("summary", {"total": 1, "counts": {"product_unavailable": 1}}),
                "item": finalized_job,
                "items": [finalized_job],
            }
        if _should_dispatch_tiktok_browser_fallback(exc=exc, job_payload=job_payload):
            result_payload = _build_tiktok_browser_fallback_required_worker_result(
                worker_params=worker_params or job_payload,
                job_payload=job_payload,
                error=exc,
            )
            finalized_job = store.mark_api_worker_job_success(
                job_id=job_id,
                run_id=run_id,
                summary=result_payload["summary"],
                result=result_payload,
                stage="browser_fallback_required",
            )
            parent_update = _mark_tiktok_fastmoss_product_ingest_ready_if_done(
                store=store,
                request_id=request_id,
            )
            return {
                "control_action": "api_worker_once",
                "daemon_status": "processed",
                "processed_count": 1,
                "success_count": 1,
                "failed_count": 0,
                "message": "API worker requested TikTok browser fallback for one product ingest job.",
                "request_id": request_id,
                "api_worker_job_id": job_id,
                "api_worker_job": finalized_job,
                "worker_result": result_payload,
                "parent_updates": [parent_update],
                "summary": finalized_job.get(
                    "summary",
                    {"total": 1, "counts": {"tiktok_browser_fallback_required": 1}},
                ),
                "item": finalized_job,
                "items": [finalized_job],
            }
        error_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        failed_job = store.mark_api_worker_job_retry_or_failed(
            job_id=job_id,
            run_id=run_id,
            error_text=error_text,
            summary=result_payload.get("summary", {}) if isinstance(result_payload, dict) else {},
            result=result_payload if isinstance(result_payload, dict) else {},
            retry_delay_seconds=float(settings["retry_delay_seconds"]),
        )
        parent_update = _mark_tiktok_fastmoss_product_ingest_ready_if_done(
            store=store,
            request_id=request_id,
        )
        final_failed = str(failed_job.get("status", "") or "") == "failed"
        return {
            "control_action": "api_worker_once",
            "daemon_status": "processed",
            "processed_count": 1,
            "success_count": 0,
            "failed_count": 1 if final_failed else 0,
            "message": "API worker failed one TikTok/FastMoss product ingest job.",
            "request_id": request_id,
            "api_worker_job_id": job_id,
            "api_worker_job": failed_job,
            "worker_result": result_payload,
            "parent_updates": [parent_update],
            "summary": failed_job.get("summary", {"total": 1, "counts": {failed_job.get("status", "failed"): 1}}),
            "item": failed_job,
            "items": [failed_job],
            "error": error_text,
        }


def _run_feishu_tk_selection_table_read_api_worker_job(
    *,
    store: RuntimeStore,
    settings: dict[str, Any],
    params: dict[str, Any],
    job: Mapping[str, Any],
) -> dict[str, Any]:
    job_id = str(job.get("job_id", "") or "")
    request_id = str(job.get("request_id", "") or "")
    run_id = str(job.get("run_id", "") or f"api-worker-{job_id}")
    result_payload: dict[str, Any] = {}
    try:
        job_payload = job.get("payload", {}) if isinstance(job.get("payload"), dict) else {}
        worker_params = {
            **params,
            **_runtime_params_from_settings(settings),
            **dict(job_payload),
            "request_id": request_id,
            "notification_channel_code": "",
            "reply_target": "",
        }
        with _CallbackHeartbeat(
            callback=lambda: store.heartbeat_api_worker_job(
                job_id=job_id,
                lease_seconds=float(settings["lease_seconds"]),
            ),
            interval_seconds=min(
                float(settings["heartbeat_interval_seconds"]),
                float(settings["lease_seconds"]),
            ),
        ):
            result_payload = read_feishu_tk_selection_table_for_product(worker_params)
        finalized_job = store.mark_api_worker_job_success(
            job_id=job_id,
            run_id=run_id,
            summary=result_payload.get("summary", {}) if isinstance(result_payload, dict) else {},
            result=result_payload if isinstance(result_payload, dict) else {},
        )
        parent_update = _mark_tiktok_fastmoss_product_ingest_ready_if_done(
            store=store,
            request_id=request_id,
        )
        return {
            "control_action": "api_worker_once",
            "daemon_status": "processed",
            "processed_count": 1,
            "success_count": 1,
            "failed_count": 0,
            "message": "API worker processed one Feishu TK selection table-read job.",
            "request_id": request_id,
            "api_worker_job_id": job_id,
            "api_worker_job": finalized_job,
            "worker_result": result_payload,
            "parent_updates": [parent_update],
            "summary": finalized_job.get("summary", {"total": 1, "counts": {"success": 1}}),
            "item": finalized_job,
            "items": [finalized_job],
        }
    except Exception as exc:
        error_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        failed_job = store.mark_api_worker_job_retry_or_failed(
            job_id=job_id,
            run_id=run_id,
            error_text=error_text,
            summary=result_payload.get("summary", {}) if isinstance(result_payload, dict) else {},
            result=result_payload if isinstance(result_payload, dict) else {},
            retry_delay_seconds=float(settings["retry_delay_seconds"]),
        )
        parent_update = _mark_tiktok_fastmoss_product_ingest_ready_if_done(
            store=store,
            request_id=request_id,
        )
        final_failed = str(failed_job.get("status", "") or "") == "failed"
        return {
            "control_action": "api_worker_once",
            "daemon_status": "processed",
            "processed_count": 1,
            "success_count": 0,
            "failed_count": 1 if final_failed else 0,
            "message": "API worker failed one Feishu TK selection table-read job.",
            "request_id": request_id,
            "api_worker_job_id": job_id,
            "api_worker_job": failed_job,
            "worker_result": result_payload,
            "parent_updates": [parent_update],
            "summary": failed_job.get("summary", {"total": 1, "counts": {failed_job.get("status", "failed"): 1}}),
            "item": failed_job,
            "items": [failed_job],
            "error": error_text,
        }


def _run_feishu_tk_selection_table_writeback_api_worker_job(
    *,
    store: RuntimeStore,
    settings: dict[str, Any],
    params: dict[str, Any],
    job: Mapping[str, Any],
) -> dict[str, Any]:
    job_id = str(job.get("job_id", "") or "")
    request_id = str(job.get("request_id", "") or "")
    run_id = str(job.get("run_id", "") or f"api-worker-{job_id}")
    result_payload: dict[str, Any] = {}
    try:
        job_payload = job.get("payload", {}) if isinstance(job.get("payload"), dict) else {}
        worker_params = {
            **params,
            **_runtime_params_from_settings(settings),
            **dict(job_payload),
            "request_id": request_id,
            "notification_channel_code": "",
            "reply_target": "",
        }
        with _CallbackHeartbeat(
            callback=lambda: store.heartbeat_api_worker_job(
                job_id=job_id,
                lease_seconds=float(settings["lease_seconds"]),
            ),
            interval_seconds=min(
                float(settings["heartbeat_interval_seconds"]),
                float(settings["lease_seconds"]),
            ),
        ):
            result_payload = writeback_feishu_tk_selection_table(worker_params)
        finalized_job = store.mark_api_worker_job_success(
            job_id=job_id,
            run_id=run_id,
            summary=result_payload.get("summary", {}) if isinstance(result_payload, dict) else {},
            result=result_payload if isinstance(result_payload, dict) else {},
        )
        parent_update = _mark_tiktok_fastmoss_product_ingest_ready_if_done(
            store=store,
            request_id=request_id,
        )
        return {
            "control_action": "api_worker_once",
            "daemon_status": "processed",
            "processed_count": 1,
            "success_count": 1,
            "failed_count": 0,
            "message": "API worker processed one Feishu TK selection table writeback job.",
            "request_id": request_id,
            "api_worker_job_id": job_id,
            "api_worker_job": finalized_job,
            "worker_result": result_payload,
            "parent_updates": [parent_update],
            "summary": finalized_job.get("summary", {"total": 1, "counts": {"success": 1}}),
            "item": finalized_job,
            "items": [finalized_job],
        }
    except Exception as exc:
        error_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        failed_job = store.mark_api_worker_job_retry_or_failed(
            job_id=job_id,
            run_id=run_id,
            error_text=error_text,
            summary=result_payload.get("summary", {}) if isinstance(result_payload, dict) else {},
            result=result_payload if isinstance(result_payload, dict) else {},
            retry_delay_seconds=float(settings["retry_delay_seconds"]),
        )
        parent_update = _mark_tiktok_fastmoss_product_ingest_ready_if_done(
            store=store,
            request_id=request_id,
        )
        final_failed = str(failed_job.get("status", "") or "") == "failed"
        return {
            "control_action": "api_worker_once",
            "daemon_status": "processed",
            "processed_count": 1,
            "success_count": 0,
            "failed_count": 1 if final_failed else 0,
            "message": "API worker failed one Feishu TK selection table writeback job.",
            "request_id": request_id,
            "api_worker_job_id": job_id,
            "api_worker_job": failed_job,
            "worker_result": result_payload,
            "parent_updates": [parent_update],
            "summary": failed_job.get("summary", {"total": 1, "counts": {failed_job.get("status", "failed"): 1}}),
            "item": failed_job,
            "items": [failed_job],
            "error": error_text,
        }


def execute_api_worker_once(params: dict[str, Any]) -> dict[str, Any]:
    settings = _phase1_settings(params)
    store = _create_store(settings)
    request_id = str(params.get("request_id", "") or "").strip()
    api_job = store.claim_next_api_worker_job(
        request_id=request_id,
        worker_id=str(settings["worker_id"]),
        lease_seconds=float(settings["lease_seconds"]),
    )
    if api_job is not None:
        job_code = str(api_job.get("job_code", "") or "")
        if job_code == FEISHU_TK_SELECTION_TABLE_READ_API_JOB_CODE:
            return _run_feishu_tk_selection_table_read_api_worker_job(
                store=store,
                settings=settings,
                params=params,
                job=api_job,
            )
        if job_code == TIKTOK_FASTMOSS_PRODUCT_INGEST_API_JOB_CODE:
            return _run_tiktok_fastmoss_product_ingest_api_worker_job(
                store=store,
                settings=settings,
                params=params,
                job=api_job,
            )
        if job_code == FEISHU_TK_SELECTION_TABLE_WRITEBACK_API_JOB_CODE:
            return _run_feishu_tk_selection_table_writeback_api_worker_job(
                store=store,
                settings=settings,
                params=params,
                job=api_job,
            )
        failed_job = store.mark_api_worker_job_retry_or_failed(
            job_id=str(api_job.get("job_id", "") or ""),
            run_id=str(api_job.get("run_id", "") or ""),
            error_text=f"Unsupported API worker job_code '{job_code}'.",
            retry_delay_seconds=float(settings["retry_delay_seconds"]),
        )
        return {
            "control_action": "api_worker_once",
            "daemon_status": "processed",
            "processed_count": 1,
            "success_count": 0,
            "failed_count": 1 if str(failed_job.get("status", "") or "") == "failed" else 0,
            "message": f"Unsupported API worker job_code '{job_code}'.",
            "request_id": str(api_job.get("request_id", "") or ""),
            "api_worker_job_id": str(api_job.get("job_id", "") or ""),
            "api_worker_job": failed_job,
            "summary": failed_job.get("summary", {}),
            "item": failed_job,
            "items": [failed_job],
        }
    if not request_id:
        request_id = store.find_next_influencer_pool_work_request_id(
            task_code=SYNC_TK_INFLUENCER_POOL_TASK_CODE,
        )
    if not request_id:
        return {
            "control_action": "api_worker_once",
            "daemon_status": "idle",
            "processed_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "message": "No API worker job is ready to run.",
            "summary": {"total": 0, "counts": {}},
            "item": {},
            "items": [],
            "request_id": "",
            "parent_updates": [],
        }

    request = store.load_task_request(request_id=request_id)
    if request.task_code != SYNC_TK_INFLUENCER_POOL_TASK_CODE:
        return {
            "control_action": "api_worker_once",
            "daemon_status": "idle",
            "processed_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "message": "No API worker job is ready to run for this request.",
            "summary": {"total": 0, "counts": {}},
            "item": {},
            "items": [],
            "request_id": request_id,
            "parent_updates": [],
        }
    worker_params = {
        **(request.payload if isinstance(request.payload, dict) else {}),
        **params,
        **_runtime_params_from_settings(settings),
        "request_id": request_id,
        "queue_mode": "worker",
        "worker_max_iterations": 1,
        "worker_stop_when_idle": True,
        "worker_max_idle_cycles": 1,
        "notification_channel_code": "",
        "reply_target": "",
    }
    result_payload = run_sync_tk_influencer_pool_sync(worker_params)
    items = result_payload.get("items", []) if isinstance(result_payload.get("items"), list) else []
    processed_job_count = len(items)
    touched_request_ids = {
        str(item.get("request_id", "") or "")
        for item in items
        if isinstance(item, Mapping) and str(item.get("request_id", "") or "")
    }
    touched_request_ids.add(request_id)
    parent_updates = [
        _mark_sync_tk_influencer_pool_ready_if_done(store=store, request_id=touched_request_id)
        for touched_request_id in sorted(touched_request_ids)
    ]
    parent_ready_count = sum(1 for update in parent_updates if update.get("updated"))
    daemon_status = "processed" if processed_job_count > 0 or parent_ready_count > 0 else "idle"
    return {
        "control_action": "api_worker_once",
        "daemon_status": daemon_status,
        "processed_count": 1 if daemon_status == "processed" else 0,
        "success_count": 1 if daemon_status == "processed" else 0,
        "failed_count": 0,
        "message": "API worker processed influencer-pool jobs."
        if daemon_status == "processed"
        else "No influencer-pool API worker job was processed.",
        "request_id": request_id,
        "worker_result": result_payload,
        "parent_updates": parent_updates,
        "summary": result_payload.get("summary", {"total": processed_job_count, "counts": {}}),
        "item": items[0] if items else {"request_id": request_id, "status": daemon_status},
        "items": items,
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


def run_executor_daemon(params: dict[str, Any]) -> dict[str, Any]:
    return _run_loop(
        once_func=execute_executor_once,
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


def run_api_worker_daemon(params: dict[str, Any]) -> dict[str, Any]:
    return _run_loop(
        once_func=execute_api_worker_once,
        action_name="api_worker_loop",
        params=params,
    )


def _run_phase1_synchronously(params: dict[str, Any]) -> dict[str, Any]:
    submitted = submit_refresh_current_competitor_table(params)
    request_id = str(submitted["request_id"])
    executor_params = dict(params)
    executor_params["request_id"] = request_id
    first_executor = execute_executor_once(executor_params)
    if first_executor.get("request_status") == "waiting_children":
        browser_params = dict(params)
        browser_params["execution_control_stop_when_idle"] = True
        browser_params["execution_control_max_idle_cycles"] = 1
        run_phase1_browser_runloop(browser_params)
        execute_executor_once(executor_params)
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


def _run_keyword_phase1_synchronously(params: dict[str, Any]) -> dict[str, Any]:
    submitted = submit_search_keyword_competitor_products(params)
    request_id = str(submitted["request_id"])
    executor_params = dict(params)
    executor_params["request_id"] = request_id
    while True:
        executor_payload = execute_executor_once(executor_params)
        request_status = str(executor_payload.get("request_status", "") or "")
        if request_status == "waiting_children":
            browser_params = dict(params)
            browser_params["execution_control_stop_when_idle"] = True
            browser_params["execution_control_max_idle_cycles"] = 1
            run_phase1_browser_runloop(browser_params)
            continue
        break
    outbox_params = dict(params)
    outbox_params["execution_control_stop_when_idle"] = True
    outbox_params["execution_control_max_idle_cycles"] = 1
    run_phase1_outbox_dispatcher(outbox_params)
    result = get_search_keyword_competitor_products_status(
        {
            **params,
            "request_id": request_id,
        }
    )
    result["control_action"] = "run"
    result["message"] = "Phase 1 keyword search task finished in synchronous compatibility mode."
    return result


def run_refresh_current_competitor_table(params: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("control_action", "run") or "run").strip().lower()
    if action == "submit":
        return submit_refresh_current_competitor_table(params)
    if action in {"status", "result"}:
        return get_refresh_current_competitor_table_status(params)
    if action == "executor_once":
        return execute_executor_once(params)
    if action == "executor_loop":
        return run_executor_daemon(params)
    if action == "browser_once":
        return execute_phase1_browser_once(params)
    if action == "browser_loop":
        return run_phase1_browser_runloop(params)
    if action == "outbox_once":
        return dispatch_phase1_outbox_once(params)
    if action == "outbox_loop":
        return run_phase1_outbox_dispatcher(params)
    return _run_phase1_synchronously(params)


def run_search_keyword_competitor_products(params: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("control_action", "run") or "run").strip().lower()
    if action == "submit":
        return submit_search_keyword_competitor_products(params)
    if action in {"status", "result"}:
        return get_search_keyword_competitor_products_status(params)
    if action == "executor_once":
        return execute_executor_once(params)
    if action == "executor_loop":
        return run_executor_daemon(params)
    if action == "browser_once":
        return execute_phase1_browser_once(params)
    if action == "browser_loop":
        return run_phase1_browser_runloop(params)
    if action == "outbox_once":
        return dispatch_phase1_outbox_once(params)
    if action == "outbox_loop":
        return run_phase1_outbox_dispatcher(params)
    return _run_keyword_phase1_synchronously(params)


def run_sync_tk_influencer_pool_request(params: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("control_action", "run") or "run").strip().lower()
    if action == "submit":
        return submit_sync_tk_influencer_pool(params)
    if action in {"status", "result"}:
        return get_sync_tk_influencer_pool_status(params)
    if action == "executor_once":
        return execute_executor_once(params)
    if action == "executor_loop":
        return run_executor_daemon(params)
    if action == "api_worker_once":
        return execute_api_worker_once(params)
    if action == "api_worker_loop":
        return run_api_worker_daemon(params)
    if action == "browser_once":
        return execute_phase1_browser_once(params)
    if action == "browser_loop":
        return run_phase1_browser_runloop(params)
    if action == "outbox_once":
        return dispatch_phase1_outbox_once(params)
    if action == "outbox_loop":
        return run_phase1_outbox_dispatcher(params)
    return run_sync_tk_influencer_pool_sync(params)


def run_tiktok_fastmoss_product_ingest_request(params: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("control_action", "run") or "run").strip().lower()
    if action == "submit":
        return submit_tiktok_fastmoss_product_ingest(params)
    if action in {"status", "result"}:
        return get_tiktok_fastmoss_product_ingest_status(params)
    if action == "executor_once":
        return execute_executor_once(params)
    if action == "executor_loop":
        return run_executor_daemon(params)
    if action == "api_worker_once":
        return execute_api_worker_once(params)
    if action == "api_worker_loop":
        return run_api_worker_daemon(params)
    if action == "browser_once":
        return execute_phase1_browser_once(params)
    if action == "browser_loop":
        return run_phase1_browser_runloop(params)
    if action == "outbox_once":
        return dispatch_phase1_outbox_once(params)
    if action == "outbox_loop":
        return run_phase1_outbox_dispatcher(params)
    return run_tiktok_fastmoss_product_ingest_sync(params)
