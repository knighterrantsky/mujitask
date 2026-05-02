from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerResult,
)
from automation_business_scaffold.contracts.handler.dispatch import api_handler_callable
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    coerce_bool,
    coerce_mapping,
    compact_dict,
    failed_result,
    first_non_empty,
    merge_fact_bundles,
    normalize_product_identity,
    partial_success_result,
    product_business_key,
    success_result,
)
from automation_business_scaffold.contracts.workflow.execution_helpers import (
    build_projection_write_payload,
)
from automation_business_scaffold.capabilities.browser.tiktok_product_fetch_handler import (
    tiktok_product_browser_fetch_handler,
)
from automation_business_scaffold.capabilities.browser.fastmoss_security_resolve_handler import (
    fastmoss_security_browser_resolve_handler,
)
from automation_business_scaffold.control_plane.supervisor.child_runner import ChildRunnerConfig
from automation_business_scaffold.control_plane.supervisor import (
    execution_supervisor as supervisor_runtime,
)
from automation_business_scaffold.infrastructure.fastmoss.visualization_renderer import (
    DEFAULT_FASTMOSS_VISUALIZATION_CHARTS,
    FastMossVisualizationRenderer,
    FastMossVisualizationRenderError,
)

feishu_table_write_handler = api_handler_callable("feishu_table_write")
fastmoss_product_fetch_handler = api_handler_callable("fastmoss_product_fetch")
media_asset_sync_handler = api_handler_callable("media_asset_sync")
fact_bundle_upsert_handler = api_handler_callable("fact_bundle_upsert")
tiktok_product_request_fetch_handler = api_handler_callable("tiktok_product_request_fetch")
run_supervised_handler = supervisor_runtime.run_supervised_handler
ExecutionSupervisorCallbacks = supervisor_runtime.ExecutionSupervisorCallbacks


def run_selection_row_refresh_flow(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    request_payload = _request_payload(payload)
    identity = normalize_product_identity({**request_payload, **payload})
    source_context = _source_context(payload)
    source_record_id = first_non_empty(payload.get("source_record_id"), source_context.get("source_record_id"))
    source_table_ref = first_non_empty(
        payload.get("target_table_ref"),
        payload.get("source_table_ref"),
        request_payload.get("target_table_ref"),
        request_payload.get("source_table_ref"),
        payload.get("table_url"),
        request_payload.get("table_url"),
    )
    business_key = first_non_empty(payload.get("business_key"), product_business_key(identity), source_record_id)

    if not business_key:
        return failed_result(
            context,
            error=build_error(
                error_type="invalid_input",
                error_code="selection_row_missing_business_key",
                message="Selection row refresh requires a stable product business key.",
                retryable=False,
                details={"source_record_id": source_record_id, "product_identity": identity},
            ),
        )

    warnings: list[str] = []
    optional_step_failed = False
    runtime_evidence: dict[str, Any] = {
        "source_record_id": source_record_id,
        "product_business_key": business_key,
        "browser_fallback_used": False,
    }
    step_timeline: list[dict[str, Any]] = []

    _emit_progress(context, "selection_row_refresh.started", details={"source_record_id": source_record_id})

    tiktok_context = _child_context(
        context,
        handler_code="tiktok_product_request_fetch",
        payload={
            **request_payload,
            **payload,
            "request_payload": request_payload,
            "source_record_id": source_record_id,
            "product_identity": identity,
            "normalized_product_url": first_non_empty(payload.get("normalized_product_url"), identity.get("normalized_product_url")),
            "source_context": source_context,
        },
        step_code="tiktok_request",
    )
    tiktok_result = tiktok_product_request_fetch_handler(tiktok_context)
    tiktok_payload = dict(tiktok_result.result)
    request_attempt = coerce_mapping(tiktok_payload.get("request_attempt"))
    if request_attempt:
        runtime_evidence["request_attempt"] = request_attempt
    step_timeline.append(
        _timeline_entry(
            "tiktok_request",
            tiktok_result,
            detail={
                "fallback_required": bool(tiktok_payload.get("fallback_required")),
                "fallback_reason": first_non_empty(tiktok_payload.get("fallback_reason")),
            },
        )
    )
    if tiktok_result.status == "failed":
        error_code = tiktok_result.error.error_code if tiktok_result.error else ""
        if error_code in {"url_invalid_domain", "url_invalid_no_product_id"}:
            url_invalid_result = _url_invalid_pipeline_result(
                context,
                identity=identity,
                source_record_id=source_record_id,
                business_key=business_key,
                step_timeline=step_timeline,
                runtime_evidence=runtime_evidence,
                reason="url_invalid",
            )
            if source_table_ref:
                _writeback_url_invalid_status(
                    context,
                    source_record_id=source_record_id,
                    business_key=business_key,
                    identity=identity,
                    request_payload=request_payload,
                    payload=payload,
                    source_table_ref=source_table_ref,
                )
            return url_invalid_result
        return _failed_pipeline_result(
            context,
            identity=identity,
            source_record_id=source_record_id,
            business_key=business_key,
            step_timeline=step_timeline,
            failed_step="tiktok_request",
            error=tiktok_result.error,
            runtime_evidence=runtime_evidence,
        )

    if tiktok_result.status == "url_invalid":
        url_invalid_result = _url_invalid_pipeline_result(
            context,
            identity=identity,
            source_record_id=source_record_id,
            business_key=business_key,
            step_timeline=step_timeline,
            runtime_evidence=runtime_evidence,
            reason="url_invalid",
        )
        if source_table_ref:
            _writeback_url_invalid_status(
                context,
                source_record_id=source_record_id,
                business_key=business_key,
                identity=identity,
                request_payload=request_payload,
                payload=payload,
                source_table_ref=source_table_ref,
            )
        return url_invalid_result

    effective_tiktok_payload = dict(tiktok_payload)
    browser_result: HandlerResult | None = None
    browser_supervisor: dict[str, Any] = {}
    if tiktok_result.status == "fallback_required" or bool(tiktok_payload.get("fallback_required")):
        runtime_evidence["browser_fallback_used"] = True
        browser_context = _child_context(
            context,
            handler_code="tiktok_product_browser_fetch",
            payload={
                **request_payload,
                **payload,
                "request_payload": request_payload,
                "source_record_id": source_record_id,
                "product_identity": identity,
                "normalized_product_url": first_non_empty(payload.get("normalized_product_url"), identity.get("normalized_product_url")),
                "product_url": first_non_empty(identity.get("normalized_product_url"), identity.get("product_url")),
                "source_context": source_context,
                "fallback_source_job_id": first_non_empty(tiktok_payload.get("fallback_source_job_id"), context.job_id),
            },
            step_code="browser_fallback",
            worker_type="browser_worker",
            runtime_table="task_execution",
            item_code="tiktok_product_browser_fetch",
        )
        browser_outcome = run_supervised_handler(
            context=browser_context,
            dispatch=tiktok_product_browser_fetch_handler,
            heartbeat_interval_seconds=0.0,
            callbacks=ExecutionSupervisorCallbacks(
                on_progress=lambda event: _forward_browser_progress(context, event),
            ),
            child_runner_config=_browser_child_runner_config(payload, request_payload=request_payload),
        )
        browser_result = browser_outcome.worker_result
        browser_supervisor = browser_outcome.to_dict()
        runtime_evidence["browser_supervisor"] = browser_supervisor
        if browser_outcome.child_runner is not None:
            runtime_evidence["browser_child_runner"] = browser_outcome.child_runner.to_dict()
        step_timeline.append(
            _timeline_entry(
                "browser_fallback",
                browser_result,
                detail={
                    "execution_mode": browser_outcome.execution_mode,
                    "progress_stage": browser_outcome.progress_stage,
                },
            )
        )
        if browser_result.status == "failed":
            return _failed_pipeline_result(
                context,
                identity=identity,
                source_record_id=source_record_id,
                business_key=business_key,
                step_timeline=step_timeline,
                failed_step="browser_fallback",
                error=browser_result.error,
                runtime_evidence=runtime_evidence,
            )
        effective_tiktok_payload = dict(browser_result.result)
    else:
        step_timeline.append(_skipped_timeline_entry("browser_fallback", reason="not_required"))

    normalized_product_result = coerce_mapping(effective_tiktok_payload.get("normalized_product_result"))
    if not normalized_product_result:
        return _failed_pipeline_result(
            context,
            identity=identity,
            source_record_id=source_record_id,
            business_key=business_key,
            step_timeline=step_timeline,
            failed_step="tiktok_request",
            error=build_error(
                error_type="request_failure",
                error_code="selection_row_missing_tiktok_result",
                message="Selection row refresh did not obtain a normalized TikTok product result.",
                retryable=True,
            ),
            runtime_evidence=runtime_evidence,
        )

    product_unavailable = _is_unavailable_product_result(normalized_product_result)
    asset_refs = _collect_asset_refs(normalized_product_result)
    media_result_payload: dict[str, Any] = {}
    if product_unavailable:
        step_timeline.append(_skipped_timeline_entry("media_sync", reason="product_unavailable"))
    elif asset_refs:
        media_context = _child_context(
            context,
            handler_code="media_asset_sync",
            payload={
                **request_payload,
                **payload,
                "request_payload": request_payload,
                "source_record_id": source_record_id,
                "asset_refs": asset_refs,
                "entity_keys": [business_key],
                "product_id": first_non_empty(identity.get("product_id")),
                "source_context": source_context,
                "sync_referenced_files": True,
                "require_materialized_assets": coerce_bool(
                    first_non_empty(
                        payload.get("require_materialized_assets"),
                        request_payload.get("require_materialized_assets"),
                        True,
                    ),
                    default=True,
                ),
            },
            step_code="media_sync",
        )
        media_result = media_asset_sync_handler(media_context)
        step_timeline.append(_timeline_entry("media_sync", media_result))
        if media_result.status == "failed":
            optional_step_failed = True
            warnings.append(first_non_empty(media_result.error.message if media_result.error else "", "Media sync failed."))
        else:
            media_result_payload = dict(media_result.result)
    else:
        step_timeline.append(_skipped_timeline_entry("media_sync", reason="no_assets"))

    fastmoss_payload: dict[str, Any] = {}
    if product_unavailable:
        step_timeline.append(_skipped_timeline_entry("fastmoss_fetch", reason="product_unavailable"))
    else:
        fastmoss_context = _child_context(
            context,
            handler_code="fastmoss_product_fetch",
            payload={
                **request_payload,
                **payload,
                "request_payload": request_payload,
                "source_record_id": source_record_id,
                "product_identity": identity,
                "source_context": source_context,
                "detail_level": first_non_empty(payload.get("detail_level"), "standard"),
                "fastmoss_overview_window_days": _first_present(
                    payload.get("fastmoss_overview_window_days"),
                    request_payload.get("fastmoss_overview_window_days"),
                    [7, 28, 90],
                ),
                "fastmoss_window_days": first_non_empty(
                    payload.get("fastmoss_window_days"),
                    request_payload.get("fastmoss_window_days"),
                    90,
                ),
                "fastmoss_sku_window_days": first_non_empty(
                    payload.get("fastmoss_sku_window_days"),
                    request_payload.get("fastmoss_sku_window_days"),
                    28,
                ),
            },
            step_code="fastmoss_fetch",
        )
        fastmoss_result = fastmoss_product_fetch_handler(fastmoss_context)
        step_timeline.append(_timeline_entry("fastmoss_fetch", fastmoss_result))
        if fastmoss_result.status == "fallback_required":
            fallback_result, fallback_supervisor = _run_fastmoss_security_browser_fallback(
                context,
                fallback_payload=dict(fastmoss_result.result),
                request_payload=request_payload,
                source_record_id=source_record_id,
                identity=identity,
                payload=payload,
            )
            runtime_evidence["fastmoss_security_browser_fallback"] = fallback_supervisor
            step_timeline.append(
                _timeline_entry(
                    "fastmoss_security_browser_fallback",
                    fallback_result,
                    detail={
                        "execution_mode": fallback_supervisor.get("execution_mode"),
                        "progress_stage": fallback_supervisor.get("progress_stage"),
                    },
                )
            )
            if fallback_result.status == "success":
                retry_context = _child_context(
                    context,
                    handler_code="fastmoss_product_fetch",
                    payload={
                        **fastmoss_context.payload,
                        "fastmoss_security_browser_fallback_attempt": 1,
                        "fallback_source_job_id": fastmoss_context.job_id,
                    },
                    step_code="fastmoss_fetch.retry_after_security_browser_fallback",
                )
                fastmoss_result = fastmoss_product_fetch_handler(retry_context)
                step_timeline.append(_timeline_entry("fastmoss_fetch_retry", fastmoss_result))
        fastmoss_payload = dict(fastmoss_result.result)
        if fastmoss_result.status in {"failed", "fallback_required"}:
            optional_step_failed = True
            warnings.append(first_non_empty(fastmoss_result.error.message if fastmoss_result.error else "", "FastMoss fetch failed."))

    fact_bundle = merge_fact_bundles(
        _fact_bundle_without_media(coerce_mapping(normalized_product_result.get("fact_bundle"))),
        coerce_mapping(media_result_payload.get("media_fact_bundle")),
        coerce_mapping(fastmoss_payload.get("product_fact_bundle")),
    )
    fact_bundle["media_assets"] = _merge_media_assets_preserving_roles(
        coerce_mapping(media_result_payload.get("media_fact_bundle")).get("media_assets"),
        coerce_mapping(fastmoss_payload.get("product_fact_bundle")).get("media_assets"),
    )
    fact_context = _child_context(
        context,
        handler_code="fact_bundle_upsert",
        payload={
            **request_payload,
            **payload,
            "request_payload": request_payload,
            "source_record_id": source_record_id,
            "product_identity": identity,
            "fact_bundle": fact_bundle,
            "observation_context": {
                "source_record_id": source_record_id,
                "product_id": first_non_empty(identity.get("product_id")),
                "normalized_product_url": first_non_empty(identity.get("normalized_product_url"), identity.get("product_url")),
            },
        },
        step_code="fact_db_upsert",
    )
    fact_result = fact_bundle_upsert_handler(fact_context)
    step_timeline.append(_timeline_entry("fact_db_upsert", fact_result))
    if fact_result.status == "failed":
        return _failed_pipeline_result(
            context,
            identity=identity,
            source_record_id=source_record_id,
            business_key=business_key,
            step_timeline=step_timeline,
            failed_step="fact_db_upsert",
            error=fact_result.error,
            runtime_evidence=runtime_evidence,
            normalized_product_result=normalized_product_result,
            product_fact_bundle=fastmoss_payload.get("product_fact_bundle"),
        )

    projection_fields: dict[str, Any] = {}
    write_result = success_result(context, result={})
    if source_table_ref:
        chart_image_paths = _render_selection_charts(
            context=context,
            product_id=first_non_empty(identity.get("product_id"), business_key),
            fact_bundle=fact_bundle,
            fastmoss_payload=fastmoss_payload,
        )
        if chart_image_paths:
            step_timeline.append(
                _timeline_entry(
                    "chart_render",
                    success_result(context, result={"chart_image_paths": chart_image_paths}),
                )
            )

        projection_fields = _build_selection_projection_fields(
            source_context=source_context,
            normalized_product_result=normalized_product_result,
            fastmoss_result=fastmoss_payload,
            media_result=media_result_payload,
            chart_image_paths=chart_image_paths,
        )
        projection_record = compact_dict(
            {
                "source_record_id": source_record_id,
                "business_entity_key": business_key,
                "product_id": first_non_empty(
                    normalized_product_result.get("product_id"),
                    coerce_mapping(normalized_product_result.get("product")).get("product_id"),
                    identity.get("product_id"),
                ),
                "product_url": first_non_empty(
                    coerce_mapping(normalized_product_result.get("product")).get("normalized_url"),
                    coerce_mapping(normalized_product_result.get("product")).get("product_url"),
                    normalized_product_result.get("normalized_product_url"),
                    identity.get("normalized_product_url"),
                    identity.get("product_url"),
                ),
                "projection_fields": projection_fields,
                "source_fields": _source_fields(source_context),
                "source_context": source_context,
            }
        )
        write_context = _child_context(
            context,
            handler_code="feishu_table_write",
            payload={
                **request_payload,
                **payload,
                **build_projection_write_payload(
                    stage_code=context.stage_code or "collect_product_data",
                    request_id=context.request_id,
                    target_table_ref=source_table_ref,
                    records=[projection_record],
                    mapper_code="selection_table_projection_mapper",
                    write_mode="fill_missing_only",
                    request_payload=request_payload,
                    source_record_id=source_record_id,
                    business_entity_key=business_key,
                ),
                "request_payload": request_payload,
            },
            step_code="feishu_writeback",
        )
        write_result = feishu_table_write_handler(write_context)
        step_timeline.append(_timeline_entry("feishu_writeback", write_result))
        if write_result.status == "failed":
            return _failed_pipeline_result(
                context,
                identity=identity,
                source_record_id=source_record_id,
                business_key=business_key,
                step_timeline=step_timeline,
                failed_step="feishu_writeback",
                error=write_result.error,
                runtime_evidence=runtime_evidence,
                normalized_product_result=normalized_product_result,
                product_fact_bundle=fastmoss_payload.get("product_fact_bundle"),
                fact_upsert=fact_result.result,
                writeback_projection={"fields": projection_fields},
            )
    else:
        step_timeline.append(_skipped_timeline_entry("feishu_writeback", reason="no_target_table_ref"))

    product_fact_bundle = dict(fact_bundle)
    product_fact_bundle["product_id"] = first_non_empty(
        product_fact_bundle.get("product_id"),
        normalized_product_result.get("product_id"),
        coerce_mapping(normalized_product_result.get("product")).get("product_id"),
        identity.get("product_id"),
    )
    row_status = "unavailable" if product_unavailable else "partial_success" if optional_step_failed or write_result.status == "skipped" else "success"
    result = {
        "source_record_id": source_record_id,
        "business_entity_key": business_key,
        "row_status": row_status,
        "normalized_product_result": normalized_product_result,
        "product_fact_bundle": product_fact_bundle,
        "fact_upsert": dict(fact_result.result),
        "writeback_projection": {"fields": projection_fields},
        "writeback_result": dict(write_result.result),
        "step_timeline": step_timeline,
        "runtime_evidence": runtime_evidence,
    }
    summary = {
        "source_record_id": source_record_id,
        "product_business_key": business_key,
        "row_status": row_status,
        "browser_fallback_used": bool(runtime_evidence.get("browser_fallback_used")),
    }
    if row_status == "partial_success":
        return partial_success_result(context, summary=summary, result=result, warnings=tuple(dict.fromkeys(warnings)))
    if warnings:
        return success_result(context, summary=summary, result=result, warnings=tuple(dict.fromkeys(warnings)))
    return success_result(context, summary=summary, result=result)


def _child_context(
    parent: HandlerContext,
    *,
    handler_code: str,
    payload: dict[str, Any],
    step_code: str,
    worker_type: str = "api_worker",
    runtime_table: str = "api_worker_job",
    item_code: str = "",
) -> HandlerContext:
    return HandlerContext(
        request_id=parent.request_id,
        job_id=f"{parent.job_id}:{step_code}",
        handler_code=handler_code,
        worker_type=worker_type,  # type: ignore[arg-type]
        runtime_table=runtime_table,  # type: ignore[arg-type]
        payload=payload,
        workflow_code=parent.workflow_code,
        stage_code=parent.stage_code,
        job_code=handler_code if worker_type == "api_worker" else "",
        item_code=item_code,
        business_key=parent.business_key,
        dedupe_key=f"{parent.dedupe_key}:{step_code}" if parent.dedupe_key else f"{parent.job_id}:{step_code}",
        resource_code=parent.resource_code,
        worker_id=parent.worker_id,
        metadata=dict(parent.metadata),
    )


def _browser_child_runner_config(payload: Mapping[str, Any], *, request_payload: Mapping[str, Any]) -> ChildRunnerConfig | None:
    mode = first_non_empty(
        payload.get("browser_child_runner_mode"),
        request_payload.get("browser_child_runner_mode"),
        "inline",
    ).lower()
    if mode != "child_process":
        return None
    timeout_seconds = _optional_float(
        first_non_empty(
            payload.get("browser_child_runner_timeout_seconds"),
            request_payload.get("browser_child_runner_timeout_seconds"),
        )
    )
    return ChildRunnerConfig(
        mode="child_process",
        timeout_seconds=timeout_seconds,
    )


def _run_fastmoss_security_browser_fallback(
    parent: HandlerContext,
    *,
    fallback_payload: Mapping[str, Any],
    request_payload: Mapping[str, Any],
    source_record_id: str,
    identity: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> tuple[HandlerResult, dict[str, Any]]:
    browser_context = _child_context(
        parent,
        handler_code="fastmoss_security_browser_resolve",
        payload={
            **dict(request_payload),
            **dict(payload),
            **dict(fallback_payload),
            "request_payload": dict(request_payload),
            "source_record_id": source_record_id,
            "product_identity": dict(identity),
            "fallback_source_job_id": first_non_empty(
                fallback_payload.get("fallback_source_job_id"),
                parent.job_id,
            ),
        },
        step_code="fastmoss_security_browser_fallback",
        worker_type="browser_worker",
        runtime_table="task_execution",
        item_code="fastmoss_security_browser_resolve",
    )
    outcome = run_supervised_handler(
        context=browser_context,
        dispatch=fastmoss_security_browser_resolve_handler,
        heartbeat_interval_seconds=0.0,
        callbacks=ExecutionSupervisorCallbacks(
            on_progress=lambda event: _forward_browser_progress(parent, event),
        ),
        child_runner_config=_browser_child_runner_config(payload, request_payload=request_payload),
    )
    return outcome.worker_result, outcome.to_dict()


def _forward_browser_progress(
    parent: HandlerContext,
    event: supervisor_runtime.ExecutionProgressEvent,
) -> None:
    _emit_progress(
        parent,
        f"browser.{event.progress_stage}",
        message=event.message,
        details=event.details,
    )


def _emit_progress(
    context: HandlerContext,
    progress_stage: str,
    *,
    message: str = "",
    details: Mapping[str, Any] | None = None,
) -> None:
    callback = context.metadata.get("progress_callback")
    if callable(callback):
        callback(progress_stage, message=message, details=dict(details or {}))


def _timeline_entry(step: str, handler_result: HandlerResult, *, detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "step": step,
        "status": handler_result.status,
    }
    if detail:
        payload.update({str(key): value for key, value in detail.items() if value not in ("", None, [], {})})
    if handler_result.error is not None:
        payload["error_type"] = handler_result.error.error_type
        payload["error_code"] = handler_result.error.error_code
    return payload


def _skipped_timeline_entry(step: str, *, reason: str) -> dict[str, Any]:
    return {
        "step": step,
        "status": "skipped",
        "reason": reason,
    }


def _failed_pipeline_result(
    context: HandlerContext,
    *,
    identity: Mapping[str, Any],
    source_record_id: str,
    business_key: str,
    step_timeline: list[dict[str, Any]],
    failed_step: str,
    error: Any,
    runtime_evidence: Mapping[str, Any],
    normalized_product_result: Mapping[str, Any] | None = None,
    product_fact_bundle: Mapping[str, Any] | None = None,
    fact_upsert: Mapping[str, Any] | None = None,
    writeback_projection: Mapping[str, Any] | None = None,
) -> HandlerResult:
    handler_error = error if hasattr(error, "error_code") else build_error(
        error_type="internal",
        error_code="selection_row_refresh_failed",
        message=str(error or "Selection row refresh failed."),
        retryable=True,
    )
    return failed_result(
        context,
        error=handler_error,
        summary={
            "source_record_id": source_record_id,
            "product_business_key": business_key,
            "row_status": "failed",
            "failed_step": failed_step,
        },
        result=compact_dict(
            {
                "source_record_id": source_record_id,
                "business_entity_key": business_key,
                "row_status": "failed",
                "failed_step": failed_step,
                "product_identity": dict(identity),
                "normalized_product_result": dict(normalized_product_result or {}),
                "product_fact_bundle": dict(product_fact_bundle or {}),
                "fact_upsert": dict(fact_upsert or {}),
                "writeback_projection": dict(writeback_projection or {}),
                "step_timeline": step_timeline,
                "runtime_evidence": dict(runtime_evidence),
            }
        ),
    )


def _url_invalid_pipeline_result(
    context: HandlerContext,
    *,
    identity: Mapping[str, Any],
    source_record_id: str,
    business_key: str,
    step_timeline: list[dict[str, Any]],
    runtime_evidence: Mapping[str, Any],
    reason: str,
) -> HandlerResult:
    return success_result(
        context,
        summary={
            "source_record_id": source_record_id,
            "product_business_key": business_key,
            "row_status": reason,
        },
        result=compact_dict(
            {
                "source_record_id": source_record_id,
                "business_entity_key": business_key,
                "row_status": reason,
                "product_identity": dict(identity),
                "step_timeline": step_timeline,
                "runtime_evidence": dict(runtime_evidence),
                "writeback_projection": {"fields": {"商品状态": "链接不可访问"}},
            }
        ),
    )


def _writeback_url_invalid_status(
    context: HandlerContext,
    *,
    source_record_id: str,
    business_key: str,
    identity: Mapping[str, Any],
    request_payload: Mapping[str, Any],
    payload: Mapping[str, Any],
    source_table_ref: str,
) -> None:
    projection_record = compact_dict(
        {
            "source_record_id": source_record_id,
            "business_entity_key": business_key,
            "product_id": first_non_empty(identity.get("product_id")),
            "product_url": first_non_empty(identity.get("normalized_product_url"), identity.get("product_url")),
            "projection_fields": {"商品状态": "链接不可访问"},
        }
    )
    write_context = _child_context(
        context,
        handler_code="feishu_table_write",
        payload={
            **dict(request_payload),
            **dict(payload),
            **build_projection_write_payload(
                stage_code=context.stage_code or "collect_selection_rows",
                request_id=context.request_id,
                target_table_ref=source_table_ref,
                records=[projection_record],
                mapper_code="selection_table_projection_mapper",
                write_mode="fill_missing_only",
                request_payload=dict(request_payload),
                source_record_id=source_record_id,
                business_entity_key=business_key,
            ),
            "request_payload": dict(request_payload),
        },
        step_code="feishu_writeback_url_invalid",
    )
    feishu_table_write_handler(write_context)


def _request_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    request_payload = coerce_mapping(payload.get("request_payload"))
    if request_payload:
        return request_payload
    return dict(payload)


def _source_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    source_context = coerce_mapping(payload.get("source_context"))
    if source_context:
        return source_context
    return dict(payload)


def _source_fields(source_context: Mapping[str, Any]) -> dict[str, Any]:
    for candidate in (
        source_context.get("source_fields"),
        source_context.get("fields"),
        coerce_mapping(source_context.get("source_context")).get("source_fields"),
        coerce_mapping(source_context.get("source_context")).get("fields"),
    ):
        if isinstance(candidate, Mapping):
            return dict(candidate)
    return {}


def _fact_bundle_without_media(fact_bundle: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = dict(coerce_mapping(fact_bundle))
    cleaned["media_assets"] = []
    return cleaned


def _merge_media_assets_preserving_roles(*asset_lists: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for asset_list in asset_lists:
        for item in asset_list if isinstance(asset_list, list) else []:
            if not isinstance(item, Mapping):
                continue
            record = dict(item)
            asset_ref = first_non_empty(
                record.get("asset_key"),
                record.get("object_key"),
                record.get("remote_uri"),
                record.get("source_url"),
                record.get("source_path"),
                record.get("local_path"),
                record.get("file_token"),
            )
            dedupe_key = f"{asset_ref}:{first_non_empty(record.get('media_role'))}" if asset_ref else ""
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            merged.append(record)
    return merged


def _collect_asset_refs(normalized_product_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in normalized_product_result.get("media_assets") if isinstance(normalized_product_result.get("media_assets"), list) else []:
        if not isinstance(item, Mapping):
            continue
        record = dict(item)
        asset_ref = first_non_empty(record.get("source_url"), record.get("local_path"), record.get("object_key"))
        dedupe_key = f"{asset_ref}:{first_non_empty(record.get('media_role'))}" if asset_ref else ""
        if not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        assets.append(record)
    return assets


def _render_selection_charts(
    *,
    context: HandlerContext,
    product_id: str,
    fact_bundle: Mapping[str, Any],
    fastmoss_payload: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    overview_payload = _extract_raw_api_payload(fact_bundle, "goods.overview", d_type=28)
    if not overview_payload:
        overview_payload = _extract_raw_api_payload(fact_bundle, "goods.overview")
    product_sku_payload = _extract_raw_api_payload(fact_bundle, "goods.skus")
    if not overview_payload and not product_sku_payload:
        overview_payload = _extract_raw_api_payload(fastmoss_payload, "goods.overview", d_type=28)
        if not overview_payload:
            overview_payload = _extract_raw_api_payload(fastmoss_payload, "goods.overview")
        product_sku_payload = _extract_raw_api_payload(fastmoss_payload, "goods.skus")

    if not overview_payload:
        return {}

    sku_distribution_payload = _extract_raw_api_payload(fact_bundle, "goods.sku_distribution")
    if not sku_distribution_payload:
        sku_distribution_payload = _extract_raw_api_payload(fastmoss_payload, "goods.sku_distribution")
    sku_payload = dict(product_sku_payload if isinstance(product_sku_payload, Mapping) else {})
    if isinstance(sku_distribution_payload, Mapping):
        sku_payload.update({k: v for k, v in sku_distribution_payload.items() if k not in sku_payload})
    try:
        renderer = FastMossVisualizationRenderer()
        result = renderer.render_product_charts(
            product_id=product_id,
            overview_payload=overview_payload,
            product_sku_payload=sku_payload,
            charts=DEFAULT_FASTMOSS_VISUALIZATION_CHARTS,
        )
    except (FastMossVisualizationRenderError, ValueError, TypeError) as exc:
        _emit_progress(context, "chart_render.failed", message=str(exc))
        return {}

    chart_map: dict[str, list[dict[str, Any]]] = {}
    for chart_name, file_path in result.files.items():
        if not file_path.exists():
            continue
        key = _CHART_NAME_TO_FIELD.get(chart_name)
        if not key:
            continue
        chart_map[key] = [
            {
                "local_path": str(file_path.resolve()),
                "file_name": f"{chart_name}.png",
                "mime_type": "image/png",
            }
        ]
    return chart_map


def _extract_raw_api_payload(payload: Mapping[str, Any], endpoint: str, *, d_type: int | None = None) -> dict[str, Any]:
    raw_responses = payload.get("raw_api_responses")
    if isinstance(raw_responses, list):
        for item in raw_responses:
            if isinstance(item, Mapping) and str(item.get("source_endpoint") or "") == endpoint:
                response_payload = item.get("response_payload")
                if not isinstance(response_payload, Mapping):
                    continue
                if d_type is not None:
                    request_params = item.get("request_params")
                    if isinstance(request_params, Mapping) and int(request_params.get("d_type") or 0) != d_type:
                        continue
                return dict(response_payload)
    return {}


def _extract_parent_sku_image(fact_bundle: Mapping[str, Any]) -> str:
    raw_responses = fact_bundle.get("raw_api_responses")
    if not isinstance(raw_responses, list):
        return ""
    for item in raw_responses:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("source_endpoint") or "") == "goods.skus":
            rp = item.get("response_payload")
            if not isinstance(rp, Mapping):
                continue
            # Try sku_detail first (older API structure)
            sku_detail = rp.get("sku_detail")
            if isinstance(sku_detail, list) and sku_detail:
                sale_prop_values = sku_detail[0].get("sale_prop_values") if isinstance(sku_detail[0], Mapping) else None
                if isinstance(sale_prop_values, list) and sale_prop_values:
                    for prop in sale_prop_values:
                        if isinstance(prop, Mapping):
                            image = first_non_empty(prop.get("image"))
                            if image:
                                return image
            # Try sku_list/list (current API structure)
            sku_list = rp.get("sku_list") or rp.get("list")
            if isinstance(sku_list, list) and sku_list:
                for sku_row in sku_list:
                    if not isinstance(sku_row, Mapping):
                        continue
                    # Check sku_sale_props for image
                    sale_props = sku_row.get("sku_sale_props") or sku_row.get("props")
                    if isinstance(sale_props, list):
                        for prop in sale_props:
                            if isinstance(prop, Mapping):
                                image = first_non_empty(prop.get("image"), prop.get("img"), prop.get("image_url"))
                                if image:
                                    return image
                    # Check direct image fields on the SKU row
                    image = first_non_empty(sku_row.get("image"), sku_row.get("img"), sku_row.get("image_url"), sku_row.get("cover"))
                    if image:
                        return image
    return ""


_CHART_NAME_TO_FIELD: dict[str, str] = {
    "marketing_strategy": "distribution_chart",
    "overview_trend": "trend_chart",
    "sku_analysis": "sku_chart",
}



def _build_selection_projection_fields(
    *,
    source_context: Mapping[str, Any],
    normalized_product_result: Mapping[str, Any],
    fastmoss_result: Mapping[str, Any],
    media_result: Mapping[str, Any],
    chart_image_paths: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    product = coerce_mapping(normalized_product_result.get("product"))
    logical_fields = coerce_mapping(normalized_product_result.get("logical_fields"))
    fastmoss_bundle = coerce_mapping(fastmoss_result.get("product_fact_bundle"))
    metrics_snapshot = coerce_mapping(fastmoss_result.get("metrics_snapshot"))
    overview_metrics = coerce_mapping(metrics_snapshot.get("overview"))
    product_skus = fastmoss_bundle.get("product_skus") if isinstance(fastmoss_bundle.get("product_skus"), list) else []
    main_image = _first_present(
        _first_media_asset_ref(media_result),
        _first_media_asset_ref(normalized_product_result),
        logical_fields.get("main_image_url"),
    )
    gallery_images = logical_fields.get("gallery_images")
    if isinstance(gallery_images, list):
        gallery_images = [item for item in gallery_images if item not in ("", None)]
    if not gallery_images:
        gallery_images = ""
    parent_spec = ""
    parent_image = ""
    if product_skus:
        first_sku = coerce_mapping(product_skus[0])
        parent_spec = first_non_empty(first_sku.get("spec_name"), first_sku.get("sku_name"))
        sku_media = first_sku.get("media_assets") if isinstance(first_sku.get("media_assets"), list) else []
        if sku_media:
            first_media = coerce_mapping(sku_media[0])
            parent_image = _first_present(
                first_media.get("source_url"),
                first_media.get("url"),
                first_media.get("file_token"),
            )
        if not parent_image:
            parent_image = _extract_parent_sku_image(fastmoss_bundle)

    chart_images = dict(chart_image_paths or {})
    review_count = _number_value(logical_fields.get("review_count"))
    rating = _number_value(logical_fields.get("rating_score"), logical_fields.get("rating"))
    price_value = _number_value(
        logical_fields.get("price_text"),
        product.get("price_text"),
        _price_number_text(
            logical_fields.get("price_text"),
            product.get("price_text"),
            overview_metrics.get("front_price"),
            overview_metrics.get("real_price"),
            overview_metrics.get("price"),
        ),
    )
    total_sales = _number_value(
        _metric_text(overview_metrics, "sales_28d", "sold_count_28d", "day28_sold_count"),
    )
    fields = {
        "商品ID": first_non_empty(product.get("product_id"), normalized_product_result.get("product_id")),
        "商品链接": first_non_empty(product.get("normalized_url"), product.get("product_url"), normalized_product_result.get("normalized_product_url")),
        "店铺名称": first_non_empty(logical_fields.get("shop_name"), product.get("shop_name")),
        "商品标题": first_non_empty(logical_fields.get("title"), product.get("title")),
        "商品当前价格": price_value if price_value is not None else "",
        "商品评论数": review_count if review_count is not None else "",
        "商品评分": rating if rating is not None else "",
        "商品描述": first_non_empty(logical_fields.get("description")),
        "商品主图": main_image,
        "商品侧边栏图片": gallery_images,
        "今年总销量": total_sales if total_sales is not None else "",
        "出单种类占比截图": chart_images.get("distribution_chart") or [],
        "今年总销量趋势截图": chart_images.get("trend_chart") or [],
        "SKU销量占比分析": chart_images.get("sku_chart") or [],
        "父体规格": parent_spec,
        "父体图片": parent_image,
    }
    if _is_unavailable_product_result(normalized_product_result):
        fields["商品状态"] = "已下架/区域不可售"
    if _source_fields(source_context):
        fields["记录日期"] = first_non_empty(source_context.get("记录日期"))
    return {key: value for key, value in fields.items() if value not in ("", None, [], {})}


def _is_unavailable_product_result(payload: Mapping[str, Any]) -> bool:
    product = coerce_mapping(payload.get("product"))
    facts = coerce_mapping(product.get("facts"))
    logical_fields = coerce_mapping(payload.get("logical_fields"))
    status_values = (
        payload.get("availability_status"),
        payload.get("status"),
        product.get("availability_status"),
        facts.get("availability_status"),
        logical_fields.get("availability_status"),
    )
    if any(str(value or "").strip().lower() == "unavailable" for value in status_values):
        return True
    product_status = first_non_empty(product.get("status"), facts.get("status"), payload.get("product_status"))
    return product_status == "off_shelf_or_region_unavailable"


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in ("", None, [], {}):
            return value
    return ""


def _first_media_asset_ref(payload: Mapping[str, Any]) -> Any:
    for key in ("synced_assets", "media_assets"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            file_token = first_non_empty(item.get("file_token"))
            local_path = first_non_empty(item.get("source_path"), item.get("local_path"))
            url = first_non_empty(item.get("remote_uri"), item.get("source_url"))
            object_key = first_non_empty(item.get("object_key"))
            if file_token or local_path or url or object_key:
                return compact_dict(
                    {
                        "file_token": file_token,
                        "local_path": local_path,
                        "url": url,
                        "source_url": first_non_empty(item.get("source_url")),
                        "remote_uri": first_non_empty(item.get("remote_uri")),
                        "object_key": object_key,
                        "file_name": first_non_empty(item.get("file_name")),
                        "mime_type": first_non_empty(item.get("mime_type")),
                    }
                )
    for nested_key in ("media_fact_bundle", "fact_bundle"):
        nested = payload.get(nested_key)
        if isinstance(nested, Mapping):
            value = _first_media_asset_ref(nested)
            if value not in ("", None, [], {}):
                return value
    return ""


def _metric_text(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key) if isinstance(payload, Mapping) else None
        text = first_non_empty(value)
        if text:
            return text
    return ""


def _daily_sales_text(daily_metrics: list[Any], *, window_days: int) -> str:
    normalized = [dict(item) for item in daily_metrics if isinstance(item, Mapping)]
    if not normalized or window_days <= 0:
        return ""
    ordered = sorted(
        normalized,
        key=lambda item: first_non_empty(item.get("metric_date"), item.get("date"), item.get("dt")),
    )
    if len(ordered) < window_days:
        return ""
    total = 0.0
    for item in ordered[-window_days:]:
        value = _number_value(
            item.get("sold_count"),
            coerce_mapping(item.get("payload")).get("inc_sold_count"),
        )
        if value is None:
            return ""
        total += value
    return str(int(total)) if float(total).is_integer() else str(total)


def _price_number_text(*values: Any) -> str:
    text = ""
    for value in values:
        candidate = first_non_empty(value)
        if not candidate:
            continue
        if "*" in candidate:
            continue
        text = candidate
        break
    if not text:
        return ""
    normalized = re.sub(r"^(?:US\$|USD\s*|\$|￥|¥|CNY\s*|RMB\s*)", "", text.strip(), flags=re.IGNORECASE).strip()
    normalized = re.sub(r"\s*(?:USD|US\$|美元|元)$", "", normalized, flags=re.IGNORECASE).strip()
    match = re.search(r"[-+]?\d+(?:\.\d+)?", normalized)
    if match is None:
        return normalized
    number = match.group(0)
    return number.rstrip("0").rstrip(".") if "." in number else number


def _number_value(*values: Any) -> float | None:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        text = first_non_empty(value).replace(",", "")
        if not text:
            continue
        try:
            return float(text)
        except ValueError:
            continue
    return None


def _optional_float(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


__all__ = ["run_selection_row_refresh_flow"]
