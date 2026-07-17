from __future__ import annotations

import time
from typing import Any

from automation_business_scaffold.control_plane.executor.request_aggregation import (
    build_runtime_request_payload,
    refresh_request_aggregate_counts,
)
from automation_business_scaffold.control_plane.executor.workflow_registry import (
    get_workflow_definition,
    load_workflow_runtime,
)
from automation_business_scaffold.control_plane.runtime_config.settings import (
    build_idle_payload,
    build_runtime_settings,
    create_runtime_store,
    ensure_formal_task_code,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

MAX_EXECUTOR_STAGE_HOPS = 16
WORKFLOW_RUNTIME_NOT_READY_MESSAGE = "No workflow runtime is registered for this task_code."


def execute_executor_once(params: dict[str, Any]) -> dict[str, Any]:
    settings = build_runtime_settings(params)
    store = create_runtime_store(settings)
    request = _claim_next_reconcilable_request(
        store=store,
        worker_id=settings.worker_id,
        lease_seconds=settings.lease_seconds,
    )
    if request is None:
        return build_idle_payload(
            control_action="executor_once",
            actor="daemon",
            message="No task_request is ready for executor processing.",
        )

    refresh_request_aggregate_counts(store, request_id=request.request_id)
    request = store.load_task_request(request_id=request.request_id)
    if getattr(request, "status", "") == "cancelling":
        outcome = store.reconcile_cancelling_request(request_id=request.request_id)
        payload = build_runtime_request_payload(
            store=store,
            request_id=request.request_id,
            control_action="executor_once",
            message="Executor reconciled cancelling task request.",
        )
        payload.update(
            {
                "daemon_status": "processed",
                "processed_count": 1,
                "success_count": 0,
                "failed_count": 0,
                "cancel": outcome,
            }
        )
        return payload
    workflow = get_workflow_definition(request.task_code)
    runtime = resolve_workflow_runtime(request.task_code)
    current_stage = str(request.current_stage or "").strip() or workflow.entry_stage_code

    if getattr(request, "status", "") == "cancelling":
        outcome = store.reconcile_cancelling_request(request_id=request.request_id)
        payload = build_runtime_request_payload(
            store=store,
            request_id=request.request_id,
            control_action="executor_once",
            message="Executor stopped normal workflow for cancelling task request.",
        )
        payload.update({"daemon_status": "processed", "processed_count": 1, "cancel": outcome})
        return payload

    if runtime is None:
        finalized = finalize_not_ready_request(
            store=store,
            request_id=request.request_id,
            current_stage=current_stage,
            message=f"{WORKFLOW_RUNTIME_NOT_READY_MESSAGE} task_code={request.task_code}",
        )
        finalized.update(
            {
                "daemon_status": "not_ready",
                "processed_count": 1,
                "success_count": 0,
                "failed_count": 1,
            }
        )
        return finalized

    details: dict[str, Any] = {}
    for _ in range(MAX_EXECUTOR_STAGE_HOPS):
        request = store.load_task_request(request_id=request.request_id)
        if getattr(request, "status", "") == "cancelling":
            outcome = store.reconcile_cancelling_request(request_id=request.request_id)
            payload = build_runtime_request_payload(
                store=store,
                request_id=request.request_id,
                control_action="executor_once",
                message="Executor stopped normal workflow for cancelling task request.",
            )
            payload.update({"daemon_status": "processed", "processed_count": 1, "cancel": outcome})
            return payload
        current_stage = str(request.current_stage or "").strip() or workflow.entry_stage_code

        if current_stage == workflow.summary_policy.summary_stage_code:
            parent_updates = runtime.release_request_after_child_completion(
                store=store,
                request_id=request.request_id,
            )
            if parent_updates:
                details["parent_updates"] = parent_updates
                continue
            payload = runtime.finalize_request(store=store, request=request, workflow=workflow)
            payload.update(
                {
                    "daemon_status": "processed",
                    "processed_count": 1,
                    "success_count": 1 if payload.get("request_status") in {"success", "partial_success"} else 0,
                    "failed_count": 1 if payload.get("request_status") == "failed" else 0,
                }
            )
            return payload

        stage_result = runtime.advance_stage(
            store=store,
            request=request,
            workflow=workflow,
            stage_code=current_stage,
        )
        details = dict(stage_result.get("details") or {})
        action = str(stage_result["action"])
        if action == "advance":
            next_stage = str(stage_result["next_stage"])
            store.update_task_request(
                request_id=request.request_id,
                status="pending",
                current_stage=next_stage,
                worker_id="",
                lease_until=0.0,
                heartbeat_at=0.0,
                error_text="",
            )
            continue
        if action == "waiting":
            waiting_stage = str(stage_result.get("current_stage") or current_stage)
            store.update_task_request(
                request_id=request.request_id,
                status="waiting",
                current_stage=waiting_stage,
                result=dict(stage_result.get("wait") or {}),
                worker_id="",
                lease_until=0.0,
                heartbeat_at=0.0,
                error_text="",
            )
            payload = build_runtime_request_payload(
                store=store,
                request_id=request.request_id,
                control_action="executor_once",
                message=str(stage_result.get("message") or "Executor dispatched runtime child work."),
            )
            payload.update(details)
            payload.update(
                {
                    "daemon_status": "processed",
                    "processed_count": 1,
                    "success_count": 1,
                    "failed_count": 0,
                }
            )
            return payload
        if action == "finalize":
            payload = runtime.finalize_request(
                store=store,
                request=store.load_task_request(request_id=request.request_id),
                workflow=workflow,
                force_result=stage_result,
            )
            payload.update(details)
            payload.update(
                {
                    "daemon_status": "processed",
                    "processed_count": 1,
                    "success_count": 1 if payload.get("request_status") in {"success", "partial_success"} else 0,
                    "failed_count": 1 if payload.get("request_status") == "failed" else 0,
                }
            )
            return payload
        raise RuntimeError(f"Unsupported executor action '{action}' for stage {current_stage}.")

    exhausted = finalize_not_ready_request(
        store=store,
        request_id=request.request_id,
        current_stage=current_stage,
        message="Executor exhausted the stage hop budget without reaching a stable state.",
    )
    exhausted.update(
        {
            "daemon_status": "failed",
            "processed_count": 1,
            "success_count": 0,
            "failed_count": 1,
        }
    )
    return exhausted


def _claim_next_reconcilable_request(
    *,
    store: RuntimeStore,
    worker_id: str,
    lease_seconds: float,
) -> Any | None:
    request = store.claim_next_task_request(worker_id=worker_id, lease_seconds=lease_seconds)
    if request is not None:
        return request

    for waiting_request in store.list_waiting_task_requests(limit=16):
        refresh_request_aggregate_counts(store, request_id=waiting_request.request_id)
        parent_updates = release_request_after_child_completion(store, request_id=waiting_request.request_id)
        if not parent_updates:
            continue
        request = store.claim_next_task_request(worker_id=worker_id, lease_seconds=lease_seconds)
        if request is not None:
            return request
    return None


def finalize_not_ready_request(
    *,
    store: RuntimeStore,
    request_id: str,
    current_stage: str,
    message: str,
) -> dict[str, Any]:
    store.update_task_request(
        request_id=request_id,
        status="finished",
        result_status="failed",
        current_stage=current_stage,
        result={"status": "not_ready", "message": message},
        summary={"total": 0, "counts": {"not_ready": 1}},
        error_text=message,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        finished_at=time.time(),
    )
    return build_runtime_request_payload(
        store=store,
        request_id=request_id,
        control_action="executor_once",
        message=message,
    )


def release_request_after_child_completion(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    if getattr(request, "status", "") == "cancelling":
        return [store.reconcile_cancelling_request(request_id=request_id)]
    if request.status in {"finished", "cancelled"}:
        return []
    runtime = resolve_workflow_runtime(request.task_code)
    if runtime is None:
        return []
    return runtime.release_request_after_child_completion(store=store, request_id=request_id)


def resolve_workflow_runtime(task_code: str) -> Any | None:
    return load_workflow_runtime(ensure_formal_task_code(task_code))
