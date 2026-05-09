from __future__ import annotations

import os
import time
from typing import Any, Mapping

from automation_business_scaffold.config import get_execution_control_defaults
from automation_business_scaffold.control_plane.executor.looping import (
    build_child_runner_config,
    run_control_loop,
    supervisor_error_payload,
)
from automation_business_scaffold.control_plane.executor.workflow_registry import load_workflow_runtime
from automation_business_scaffold.control_plane.runtime_config.settings import (
    FORMAL_TASK_CODES,
    INFLUENCER_POOL_TASK_CODE,
    KEYWORD_TASK_CODE,
    PRODUCT_INGEST_TASK_CODE,
    SELECTION_KEYWORD_TASK_CODE,
    REFRESH_COMPETITOR_ROW_BY_URL_TASK_CODE,
    REFRESH_TASK_CODE,
    build_idle_payload,
    build_request_payload,
    build_runtime_settings,
    create_runtime_store,
    ensure_formal_task_code,
    normalize_control_action,
)
from automation_business_scaffold.control_plane.supervisor.execution_supervisor import (
    ExecutionSupervisorCallbacks,
    ExecutionSupervisorOutcome,
    run_supervised_handler,
)
from automation_business_scaffold.contracts.handler.contract import HandlerContext
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition
from automation_business_scaffold.infrastructure.artifacts.artifact_store import normalize_artifact_store_provider
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

ACTIVE_API_JOB_STATUSES = {"pending", "running"}
ACTIVE_EXECUTION_STATUSES = {"pending", "running"}
MAX_EXECUTOR_STAGE_HOPS = 16
WORKFLOW_RUNTIME_NOT_READY_MESSAGE = "No workflow runtime is registered for this task_code."
API_HANDLER_REGISTRY: Any | None = None
BROWSER_HANDLER_REGISTRY: Any | None = None
STRICT_PERSISTENCE_TASK_CODES = set(FORMAL_TASK_CODES)
TEST_PERSISTENCE_OVERRIDE_FLAG = "allow_test_persistence_overrides"
FORMAL_SUBMIT_RUNTIME_CONFIG_FIELDS = {
    "artifact_bucket",
    "artifact_object_prefix",
    "artifact_root",
    "artifact_store",
    "artifact_store_provider",
    "BROWSER_PROFILES_FILE",
    "BROWSER_PROFILE_ID",
    "BROWSER_PROFILE_REF",
    "BROWSER_PROVIDER_NAME",
    "BROWSER_WORKSPACE_ID",
    "browser_cookies",
    "browser_profile_id",
    "browser_profile_ref",
    "browser_provider_name",
    "browser_workspace_id",
    "db_url",
    "DEFAULT_PROFILE_REF",
    "execution_control_artifact_bucket",
    "execution_control_artifact_object_prefix",
    "execution_control_artifact_root",
    "execution_control_artifact_store_provider",
    "execution_control_db_url",
    "execution_control_fact_db_url",
    "execution_control_minio_access_key",
    "execution_control_minio_create_bucket",
    "execution_control_minio_endpoint",
    "execution_control_minio_region",
    "execution_control_minio_secret_key",
    "execution_control_minio_secure",
    "fact_db_url",
    "minio_access_key",
    "minio_create_bucket",
    "minio_endpoint",
    "minio_region",
    "minio_secret_key",
    "minio_secure",
    "persistence",
    "run_mode",
    "s3_access_key",
    "s3_secret_key",
}
FORMAL_PAYLOAD_RUNTIME_CONFIG_FIELDS = FORMAL_SUBMIT_RUNTIME_CONFIG_FIELDS | {
    TEST_PERSISTENCE_OVERRIDE_FLAG,
    "run_mode",
}


def submit_task_request(task_code: str, params: dict[str, Any]) -> dict[str, Any]:
    normalized_task_code = ensure_formal_task_code(task_code)
    settings = build_runtime_settings(params)
    persistence_preflight = _strict_persistence_submit_preflight(
        task_code=normalized_task_code,
        params=params,
        settings=settings,
    )
    if persistence_preflight:
        return _rejected_submit_payload(
            task_code=normalized_task_code,
            error_type="configuration",
            error_code="strict_persistence_config_missing",
            message=persistence_preflight["message"],
            retryable=False,
            result=persistence_preflight,
        )
    store = create_runtime_store(settings)
    preflight = _runtime_db_health_preflight(store=store, settings=settings)
    if preflight:
        return _rejected_submit_payload(
            task_code=normalized_task_code,
            error_type="infrastructure",
            error_code="runtime_db_connection_unhealthy",
            message=preflight["message"],
            retryable=True,
            result={"db_connection_health": preflight["db_connection_health"]},
        )
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code=normalized_task_code,
        payload=_sanitize_task_payload(params, task_code=normalized_task_code, settings=settings),
        requested_by=settings.requested_by,
        trigger_mode=str(params.get("trigger_mode") or "manual"),
        source_channel_code=str(params.get("notification_channel_code") or params.get("source_channel_code") or "noop"),
        source_session_id=str(params.get("source_session_id") or ""),
        reply_target=str(params.get("reply_target") or ""),
        idempotency_key=str(params.get("idempotency_key") or "").strip(),
    )
    if not str(request.current_stage or "").strip():
        store.update_task_request(
            request_id=request.request_id,
            current_stage=_initial_stage_for_task_code(normalized_task_code),
        )
    _refresh_request_aggregate_counts(store, request_id=request.request_id)
    return build_request_payload(
        store=store,
        request_id=request.request_id,
        control_action="submit",
        message=f"Accepted {normalized_task_code} task request.",
    )


def get_task_request_status(task_code: str, params: dict[str, Any]) -> dict[str, Any]:
    del task_code
    request_id = str(params.get("request_id") or "").strip()
    if not request_id:
        raise ValueError("request_id is required for status/result.")
    settings = build_runtime_settings(params)
    store = create_runtime_store(settings)
    _refresh_request_aggregate_counts(store, request_id=request_id)
    return build_request_payload(
        store=store,
        request_id=request_id,
        control_action=normalize_control_action(params.get("control_action")),
        message="Loaded task request status.",
    )


def run_task_request(task_code: str, params: dict[str, Any]) -> dict[str, Any]:
    normalized_task_code = ensure_formal_task_code(task_code)
    action = normalize_control_action(params.get("control_action"))
    if action == "submit":
        return submit_task_request(normalized_task_code, params)
    if action in {"status", "result"}:
        return get_task_request_status(normalized_task_code, params)
    if action == "executor_once":
        return execute_executor_once(params)
    if action == "api_worker_once":
        return execute_api_worker_once(params)
    if action == "browser_once":
        return execute_browser_once(params)
    if action == "browser_loop":
        return run_browser_runloop(params)
    if action == "outbox_once":
        return dispatch_outbox_once(params)
    if action == "outbox_loop":
        return run_outbox_dispatcher(params)
    raise ValueError(f"Unsupported control_action '{action}' for {normalized_task_code}.")


def dispatch_outbox_once(params: dict[str, Any]) -> dict[str, Any]:
    from automation_business_scaffold.control_plane.outbox.dispatcher import (
        dispatch_outbox_once as _dispatch_outbox_once,
    )

    return _dispatch_outbox_once(params)


def ensure_request_outbox(*args: Any, **kwargs: Any) -> Any:
    from automation_business_scaffold.control_plane.outbox.dispatcher import (
        ensure_request_outbox as _ensure_request_outbox,
    )

    return _ensure_request_outbox(*args, **kwargs)


def run_outbox_dispatcher(params: dict[str, Any]) -> dict[str, Any]:
    from automation_business_scaffold.control_plane.outbox.dispatcher import (
        run_outbox_dispatcher as _run_outbox_dispatcher,
    )

    return _run_outbox_dispatcher(params)


def run_refresh_current_competitor_table_request(params: dict[str, Any]) -> dict[str, Any]:
    return run_task_request(REFRESH_TASK_CODE, params)


def run_refresh_competitor_row_by_url_request(params: dict[str, Any]) -> dict[str, Any]:
    return run_task_request(REFRESH_COMPETITOR_ROW_BY_URL_TASK_CODE, params)


def run_search_keyword_competitor_products_request(params: dict[str, Any]) -> dict[str, Any]:
    return run_task_request(KEYWORD_TASK_CODE, params)


def run_search_keyword_selection_products_request(params: dict[str, Any]) -> dict[str, Any]:
    return run_task_request(SELECTION_KEYWORD_TASK_CODE, params)


def run_sync_tk_influencer_pool_request(params: dict[str, Any]) -> dict[str, Any]:
    return run_task_request(INFLUENCER_POOL_TASK_CODE, params)


def run_tiktok_fastmoss_product_ingest_request(params: dict[str, Any]) -> dict[str, Any]:
    return run_task_request(PRODUCT_INGEST_TASK_CODE, params)


def _dispatch_api_runtime_handler(context: HandlerContext) -> Any:
    return _build_bound_api_handler_registry().dispatch(context.handler_code, context)


def _dispatch_browser_runtime_handler(context: HandlerContext) -> Any:
    return _build_bound_browser_handler_registry().dispatch(context.handler_code, context)


def execute_executor_once(params: dict[str, Any]) -> dict[str, Any]:
    from automation_business_scaffold.control_plane.executor.request_dispatch import (
        execute_executor_once as _execute_executor_once,
    )

    return _execute_executor_once(params)


def run_executor_daemon(params: dict[str, Any]) -> dict[str, Any]:
    return run_control_loop(
        params=params,
        actor="daemon",
        once_func=execute_executor_once,
        idle_status_key="daemon_status",
    )


def execute_api_worker_once(params: dict[str, Any]) -> dict[str, Any]:
    from automation_business_scaffold.control_plane.executor.worker_dispatch import (
        execute_api_worker_once as _execute_api_worker_once,
    )

    return _execute_api_worker_once(params)


def run_api_worker_daemon(params: dict[str, Any]) -> dict[str, Any]:
    return run_control_loop(
        params=params,
        actor="daemon",
        once_func=execute_api_worker_once,
        idle_status_key="daemon_status",
    )


def execute_browser_once(params: dict[str, Any]) -> dict[str, Any]:
    from automation_business_scaffold.control_plane.executor.worker_dispatch import (
        execute_browser_once as _execute_browser_once,
    )

    return _execute_browser_once(params)


def run_browser_runloop(params: dict[str, Any]) -> dict[str, Any]:
    return run_control_loop(
        params=params,
        actor="daemon",
        once_func=execute_browser_once,
        idle_status_key="daemon_status",
    )


def _persist_api_worker_outcome(
    *,
    store: RuntimeStore,
    job_id: str,
    run_id: str,
    outcome: ExecutionSupervisorOutcome,
    retry_delay_seconds: float,
) -> tuple[dict[str, Any], int, int]:
    from automation_business_scaffold.control_plane.executor.worker_dispatch import persist_api_worker_outcome

    return persist_api_worker_outcome(
        store=store,
        job_id=job_id,
        run_id=run_id,
        outcome=outcome,
        retry_delay_seconds=retry_delay_seconds,
    )


def _persist_browser_execution_outcome(
    *,
    store: RuntimeStore,
    execution_id: str,
    run_id: str,
    outcome: ExecutionSupervisorOutcome,
    retry_delay_seconds: float,
) -> tuple[Any, int, int]:
    from automation_business_scaffold.control_plane.executor.worker_dispatch import persist_browser_execution_outcome

    return persist_browser_execution_outcome(
        store=store,
        execution_id=execution_id,
        run_id=run_id,
        outcome=outcome,
        retry_delay_seconds=retry_delay_seconds,
    )


def _sanitize_task_payload(
    params: dict[str, Any],
    *,
    task_code: str = "",
    settings: Any | None = None,
) -> dict[str, Any]:
    sanitized = dict(params)
    sanitized.pop("control_action", None)
    for key in FORMAL_PAYLOAD_RUNTIME_CONFIG_FIELDS:
        sanitized.pop(key, None)
    if task_code in STRICT_PERSISTENCE_TASK_CODES and settings is not None:
        _enrich_strict_persistence_payload(sanitized, params=params, settings=settings)
    return sanitized


def _strict_persistence_submit_preflight(
    *,
    task_code: str,
    params: Mapping[str, Any],
    settings: Any,
) -> dict[str, Any]:
    if task_code not in STRICT_PERSISTENCE_TASK_CODES:
        return {}
    allow_test_overrides = _test_persistence_overrides_allowed(params)
    forbidden_fields = _forbidden_formal_submit_runtime_config_fields(
        params,
        allow_test_overrides=allow_test_overrides,
    )
    if forbidden_fields:
        return {
            "message": (
                "Formal workflow submit payload must not carry runtime persistence configuration; "
                "Skill/CLI submit should pass business inputs only and let project runtime config resolve "
                "Runtime DB, Fact DB, and object storage. Forbidden fields: "
                + ", ".join(forbidden_fields)
                + "."
            ),
            "forbidden_runtime_config_fields": forbidden_fields,
            "resolved_config": {
                "runtime_db_configured": bool(getattr(settings, "db_url", "")),
                "fact_db_configured": bool(get_execution_control_defaults().fact_db_url),
            },
        }

    resolved = _resolve_submit_persistence_config(
        params,
        settings=settings,
        allow_test_overrides=allow_test_overrides,
    )
    missing: list[str] = []
    if not resolved["runtime_db_url"]:
        missing.append("Runtime DB URL")
    if not resolved["fact_db_url"]:
        missing.append("Fact DB URL")

    provider = normalize_artifact_store_provider(resolved["artifact_store_provider"])
    if provider == "local":
        missing.append("object storage provider")
    if not resolved["artifact_bucket"]:
        missing.append("object storage bucket")
    if provider == "minio":
        for field, label in (
            ("minio_endpoint", "MinIO/S3 endpoint"),
            ("minio_access_key", "MinIO/S3 access key"),
            ("minio_secret_key", "MinIO/S3 secret key"),
        ):
            if not resolved[field]:
                missing.append(label)

    if not missing:
        return {}
    return {
        "message": (
            "Formal workflow submit requires real persistence configuration; missing "
            + ", ".join(missing)
            + ". Dry-run Fact DB and local artifact success are not allowed for formal submits."
        ),
        "missing_required_config": missing,
        "resolved_config": {
            "runtime_db_configured": bool(resolved["runtime_db_url"]),
            "fact_db_configured": bool(resolved["fact_db_url"]),
            "artifact_store_provider": provider,
            "artifact_bucket_configured": bool(resolved["artifact_bucket"]),
            "minio_endpoint_configured": bool(resolved["minio_endpoint"]),
            "minio_access_key_configured": bool(resolved["minio_access_key"]),
            "minio_secret_key_configured": bool(resolved["minio_secret_key"]),
        },
    }

def _resolve_submit_persistence_config(
    params: Mapping[str, Any],
    *,
    settings: Any,
    allow_test_overrides: bool,
) -> dict[str, Any]:
    defaults = get_execution_control_defaults()
    override_params: Mapping[str, Any] = params if allow_test_overrides else {}
    artifact_store = _mapping_param(override_params.get("artifact_store"))
    persistence = _mapping_param(override_params.get("persistence"))
    return {
        "config_source": "test_submit_override" if allow_test_overrides else "project_runtime_config",
        "runtime_db_url": _first_text(override_params.get("execution_control_db_url"), getattr(settings, "db_url", "")),
        "fact_db_url": _first_text(
            override_params.get("fact_db_url"),
            override_params.get("execution_control_fact_db_url"),
            persistence.get("fact_db_url"),
            defaults.fact_db_url,
        ),
        "artifact_store_provider": _first_text(
            override_params.get("artifact_store_provider"),
            override_params.get("execution_control_artifact_store_provider"),
            artifact_store.get("artifact_store_provider"),
            artifact_store.get("provider"),
            defaults.artifact_store_provider,
        ),
        "artifact_bucket": _first_text(
            override_params.get("artifact_bucket"),
            override_params.get("execution_control_artifact_bucket"),
            artifact_store.get("artifact_bucket"),
            artifact_store.get("bucket"),
            defaults.artifact_bucket,
        ),
        "artifact_object_prefix": _first_text(
            override_params.get("artifact_object_prefix"),
            override_params.get("execution_control_artifact_object_prefix"),
            artifact_store.get("artifact_object_prefix"),
            artifact_store.get("object_prefix"),
            defaults.artifact_object_prefix,
        ),
        "artifact_root": _first_text(
            override_params.get("artifact_root"),
            override_params.get("execution_control_artifact_root"),
            artifact_store.get("artifact_root"),
            defaults.artifact_root,
        ),
        "minio_endpoint": _first_text(
            override_params.get("minio_endpoint"),
            override_params.get("execution_control_minio_endpoint"),
            artifact_store.get("minio_endpoint"),
            defaults.minio_endpoint,
        ),
        "minio_access_key": _first_text(
            override_params.get("minio_access_key"),
            override_params.get("execution_control_minio_access_key"),
            artifact_store.get("minio_access_key"),
            defaults.minio_access_key,
        ),
        "minio_secret_key": _first_text(
            override_params.get("minio_secret_key"),
            override_params.get("execution_control_minio_secret_key"),
            artifact_store.get("minio_secret_key"),
            defaults.minio_secret_key,
        ),
        "minio_region": _first_text(
            override_params.get("minio_region"),
            override_params.get("execution_control_minio_region"),
            artifact_store.get("minio_region"),
            defaults.minio_region,
        ),
        "minio_secure": _first_text(
            override_params.get("minio_secure"),
            override_params.get("execution_control_minio_secure"),
            artifact_store.get("minio_secure"),
            defaults.minio_secure,
        ),
        "minio_create_bucket": _first_text(
            override_params.get("minio_create_bucket"),
            override_params.get("execution_control_minio_create_bucket"),
            artifact_store.get("minio_create_bucket"),
            defaults.minio_create_bucket,
        ),
    }


def _enrich_strict_persistence_payload(
    payload: dict[str, Any],
    *,
    params: Mapping[str, Any],
    settings: Any,
) -> None:
    resolved = _resolve_submit_persistence_config(
        params,
        settings=settings,
        allow_test_overrides=_test_persistence_overrides_allowed(params),
    )
    payload.setdefault("requires_fact_db", True)
    payload.setdefault("requires_object_storage", True)
    payload.setdefault("require_database_persistence", True)
    payload.setdefault("require_object_storage", True)
    payload["runtime_config_source"] = resolved["config_source"]
    payload["persistence"] = {
        "requires_fact_db": True,
        "require_database_persistence": True,
        "runtime_db_configured": bool(resolved["runtime_db_url"]),
        "fact_db_configured": bool(resolved["fact_db_url"]),
        "config_source": resolved["config_source"],
    }
    payload["artifact_store"] = {
        "requires_object_storage": True,
        "require_object_storage": True,
        "artifact_store_provider": normalize_artifact_store_provider(resolved["artifact_store_provider"]),
        "provider": normalize_artifact_store_provider(resolved["artifact_store_provider"]),
        "artifact_bucket": resolved["artifact_bucket"],
        "bucket": resolved["artifact_bucket"],
        "artifact_object_prefix": resolved["artifact_object_prefix"],
        "object_prefix": resolved["artifact_object_prefix"],
        "config_source": resolved["config_source"],
    }


def _rejected_submit_payload(
    *,
    task_code: str,
    error_type: str,
    error_code: str,
    message: str,
    retryable: bool,
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result_payload = dict(result or {})
    payload = {
        "status": "failed",
        "control_action": "submit",
        "request_id": "",
        "task_code": task_code,
        "request_status": "rejected",
        "current_stage": "",
        "message": message,
        "error": message,
        "error_type": error_type,
        "error_code": error_code,
        "retryable": retryable,
        "summary": {"total": 0, "counts": {"rejected": 1}},
        "result": result_payload,
        "item": {},
        "items": [],
    }
    payload.update(result_payload)
    return payload


def _mapping_param(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _test_persistence_overrides_allowed(params: Mapping[str, Any]) -> bool:
    return _coerce_bool_param(params.get(TEST_PERSISTENCE_OVERRIDE_FLAG))


def _forbidden_formal_submit_runtime_config_fields(
    params: Mapping[str, Any],
    *,
    allow_test_overrides: bool,
) -> list[str]:
    if allow_test_overrides:
        return []
    forbidden: set[str] = set()

    def visit(value: Any, *, path: str = "") -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_text = str(key)
                child_path = f"{path}.{key_text}" if path else key_text
                if key_text in FORMAL_SUBMIT_RUNTIME_CONFIG_FIELDS and child not in (None, "", [], {}):
                    forbidden.add(child_path)
                visit(child, path=child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path=f"{path}[{index}]" if path else f"[{index}]")

    visit(params)
    return sorted(forbidden)


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _coerce_bool_param(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _runtime_db_health_preflight(*, store: RuntimeStore, settings: Any) -> dict[str, Any]:
    if not bool(getattr(settings, "db_health_preflight_enabled", True)):
        return {}
    health = store.collect_db_connection_health(
        max_connection_ratio=float(getattr(settings, "db_health_max_connection_ratio", 0.8) or 0.8),
        max_idle_in_transaction=int(getattr(settings, "db_health_max_idle_in_transaction", -1)),
    )
    if bool(health.get("healthy", False)):
        return {}
    warnings = ", ".join(str(item) for item in health.get("warnings", []) or [])
    return {
        "message": f"Runtime DB connection health check failed: {warnings or 'unhealthy'}.",
        "db_connection_health": health,
    }


def _build_runtime_request_payload(
    *,
    store: RuntimeStore,
    request_id: str,
    control_action: str,
    message: str,
) -> dict[str, Any]:
    from automation_business_scaffold.control_plane.executor.request_aggregation import build_runtime_request_payload

    return build_runtime_request_payload(
        store=store,
        request_id=request_id,
        control_action=control_action,
        message=message,
    )


def _finalize_not_ready_request(
    *,
    store: RuntimeStore,
    request_id: str,
    current_stage: str,
    message: str,
) -> dict[str, Any]:
    from automation_business_scaffold.control_plane.executor.request_dispatch import finalize_not_ready_request

    return finalize_not_ready_request(
        store=store,
        request_id=request_id,
        current_stage=current_stage,
        message=message,
    )


def _release_request_after_child_completion(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    from automation_business_scaffold.control_plane.executor.request_dispatch import release_request_after_child_completion

    return release_request_after_child_completion(store, request_id=request_id)


def _resolve_workflow_runtime(task_code: str) -> Any | None:
    from automation_business_scaffold.control_plane.executor.request_dispatch import resolve_workflow_runtime

    return resolve_workflow_runtime(task_code)


def _refresh_request_aggregate_counts(store: RuntimeStore, *, request_id: str) -> None:
    from automation_business_scaffold.control_plane.executor.request_aggregation import refresh_request_aggregate_counts

    refresh_request_aggregate_counts(store, request_id=request_id)


def _aggregate_request_children(store: RuntimeStore, *, request_id: str) -> dict[str, Any]:
    from automation_business_scaffold.control_plane.executor.request_aggregation import aggregate_request_children

    return aggregate_request_children(store, request_id=request_id)


def _handler_status_from_api_job(job: Mapping[str, Any] | None) -> str:
    if not job:
        return ""
    handler_result = _job_handler_result(job)
    return str(handler_result.get("status") or job.get("result_status") or job.get("status") or "")


def _handler_status_from_execution(execution: Any) -> str:
    if execution is None:
        return ""
    result = dict(execution.result or {})
    handler_result = result.get("handler_result")
    if isinstance(handler_result, Mapping):
        return str(handler_result.get("status") or getattr(execution, "result_status", "") or execution.status or "")
    return str(getattr(execution, "result_status", "") or execution.status or "")


def _job_handler_result(job: Mapping[str, Any] | None) -> dict[str, Any]:
    if not job:
        return {}
    result = dict(job.get("result") or {})
    handler_result = result.get("handler_result")
    return dict(handler_result or {}) if isinstance(handler_result, Mapping) else {}


def _api_worker_stage_from_handler_result(status: str) -> str:
    mapping = {
        "success": "completed",
        "partial_success": "partial_success",
        "skipped": "skipped",
        "fallback_required": "browser_fallback_required",
        "failed": "failed",
    }
    return mapping.get(status, status or "completed")


def _build_bound_api_handler_registry() -> Any:
    if API_HANDLER_REGISTRY is not None:
        return API_HANDLER_REGISTRY

    from automation_business_scaffold.contracts.handler.api import build_bound_api_handler_registry

    return build_bound_api_handler_registry()


def _build_bound_browser_handler_registry() -> Any:
    if BROWSER_HANDLER_REGISTRY is not None:
        return BROWSER_HANDLER_REGISTRY

    from automation_business_scaffold.contracts.handler.browser import (
        build_bound_browser_handler_registry,
    )

    return build_bound_browser_handler_registry()


def _initial_stage_for_task_code(task_code: str) -> str:
    normalized = ensure_formal_task_code(task_code)
    return get_workflow_definition(normalized).entry_stage_code


__all__ = [
    "FORMAL_TASK_CODES",
    "dispatch_outbox_once",
    "ensure_request_outbox",
    "execute_api_worker_once",
    "execute_browser_once",
    "execute_executor_once",
    "get_task_request_status",
    "run_api_worker_daemon",
    "run_browser_runloop",
    "run_executor_daemon",
    "run_outbox_dispatcher",
    "run_refresh_current_competitor_table_request",
    "run_search_keyword_competitor_products_request",
    "run_sync_tk_influencer_pool_request",
    "run_task_request",
    "run_tiktok_fastmoss_product_ingest_request",
    "submit_task_request",
]
