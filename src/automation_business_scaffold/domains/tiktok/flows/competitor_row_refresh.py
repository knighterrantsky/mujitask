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
from automation_business_scaffold.control_plane.supervisor.child_runner import ChildRunnerConfig
from automation_business_scaffold.control_plane.supervisor import (
    execution_supervisor as supervisor_runtime,
)

tiktok_product_browser_fetch_handler = api_handler_callable("tiktok_product_browser_fetch")
feishu_table_write_handler = api_handler_callable("feishu_table_write")
fastmoss_product_fetch_handler = api_handler_callable("fastmoss_product_fetch")
media_asset_sync_handler = api_handler_callable("media_asset_sync")
fact_bundle_upsert_handler = api_handler_callable("fact_bundle_upsert")
tiktok_product_request_fetch_handler = api_handler_callable("tiktok_product_request_fetch")
run_supervised_handler = supervisor_runtime.run_supervised_handler
ExecutionSupervisorCallbacks = supervisor_runtime.ExecutionSupervisorCallbacks


def run_competitor_row_refresh_flow(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    request_payload = _request_payload(payload)
    identity = normalize_product_identity({**request_payload, **payload})
    source_context = _source_context(payload)
    source_record_id = first_non_empty(payload.get("source_record_id"), source_context.get("source_record_id"))
    source_table_ref = first_non_empty(
        payload.get("source_table_ref"),
        request_payload.get("source_table_ref"),
        payload.get("table_url"),
        request_payload.get("table_url"),
    )
    business_key = first_non_empty(payload.get("business_key"), product_business_key(identity), source_record_id)

    if not source_record_id:
        return failed_result(
            context,
            error=build_error(
                error_type="invalid_input",
                error_code="competitor_row_missing_source_record_id",
                message="Competitor row refresh requires source_record_id.",
                retryable=False,
                details={"product_identity": identity},
            ),
        )
    if not business_key:
        return failed_result(
            context,
            error=build_error(
                error_type="invalid_input",
                error_code="competitor_row_missing_business_key",
                message="Competitor row refresh requires a stable product business key.",
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

    _emit_progress(context, "competitor_row_refresh.started", details={"source_record_id": source_record_id})

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
                error_code="competitor_row_missing_tiktok_result",
                message="Competitor row refresh did not obtain a normalized TikTok product result.",
                retryable=True,
            ),
            runtime_evidence=runtime_evidence,
        )

    asset_refs = _collect_asset_refs(normalized_product_result)
    media_result_payload: dict[str, Any] = {}
    if asset_refs:
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
            "fastmoss_window_days": first_non_empty(
                payload.get("fastmoss_window_days"),
                request_payload.get("fastmoss_window_days"),
                90,
            ),
        },
        step_code="fastmoss_fetch",
    )
    fastmoss_result = fastmoss_product_fetch_handler(fastmoss_context)
    step_timeline.append(_timeline_entry("fastmoss_fetch", fastmoss_result))
    fastmoss_payload = dict(fastmoss_result.result)
    if fastmoss_result.status == "failed":
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

    if not source_table_ref:
        return _failed_pipeline_result(
            context,
            identity=identity,
            source_record_id=source_record_id,
            business_key=business_key,
            step_timeline=step_timeline,
            failed_step="feishu_writeback",
            error=build_error(
                error_type="invalid_input",
                error_code="competitor_row_missing_source_table_ref",
                message="Competitor row refresh requires source_table_ref for Feishu writeback.",
                retryable=False,
                details={"source_record_id": source_record_id},
            ),
            runtime_evidence=runtime_evidence,
            normalized_product_result=normalized_product_result,
            product_fact_bundle=fastmoss_payload.get("product_fact_bundle"),
            fact_upsert=fact_result.result,
        )

    projection_fields = _build_competitor_projection_fields(
        source_context=source_context,
        normalized_product_result=normalized_product_result,
        fastmoss_result=fastmoss_payload,
        media_result=media_result_payload,
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
                mapper_code="competitor_table_projection_mapper",
                write_mode="upsert",
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

    product_fact_bundle = dict(fact_bundle)
    product_fact_bundle["product_id"] = first_non_empty(
        product_fact_bundle.get("product_id"),
        normalized_product_result.get("product_id"),
        coerce_mapping(normalized_product_result.get("product")).get("product_id"),
        identity.get("product_id"),
    )
    row_status = "partial_success" if optional_step_failed or write_result.status == "skipped" else "success"
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
        error_code="competitor_row_refresh_failed",
        message=str(error or "Competitor row refresh failed."),
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


def _build_competitor_projection_fields(
    *,
    source_context: Mapping[str, Any],
    normalized_product_result: Mapping[str, Any],
    fastmoss_result: Mapping[str, Any],
    media_result: Mapping[str, Any],
) -> dict[str, Any]:
    product = coerce_mapping(normalized_product_result.get("product"))
    logical_fields = coerce_mapping(normalized_product_result.get("logical_fields"))
    fastmoss_bundle = coerce_mapping(fastmoss_result.get("product_fact_bundle"))
    metrics_snapshot = coerce_mapping(fastmoss_result.get("metrics_snapshot"))
    overview_metrics = coerce_mapping(metrics_snapshot.get("overview"))
    daily_metrics = fastmoss_bundle.get("product_daily_metrics") if isinstance(fastmoss_bundle.get("product_daily_metrics"), list) else []
    main_image = _first_present(
        _first_media_asset_ref(media_result),
        _first_media_asset_ref(normalized_product_result),
        logical_fields.get("main_image_url"),
    )

    fields = {
        "SKU-ID": first_non_empty(product.get("product_id"), normalized_product_result.get("product_id")),
        "产品链接": first_non_empty(product.get("normalized_url"), product.get("product_url"), normalized_product_result.get("normalized_product_url")),
        "图片": main_image,
        "标题": first_non_empty(logical_fields.get("title"), product.get("title")),
        "节日": first_non_empty(logical_fields.get("holiday"), product.get("holiday")),
        "卖家": first_non_empty(logical_fields.get("shop_name"), product.get("seller_name"), product.get("shop_name")),
        "价格": _price_number_text(
            logical_fields.get("price_text"),
            product.get("price_text"),
            product.get("price_amount"),
            overview_metrics.get("front_price"),
            overview_metrics.get("real_price"),
            overview_metrics.get("price"),
        ),
        "Fastmoss价格": _price_number_text(
            overview_metrics.get("fastmoss_price"),
            overview_metrics.get("real_price"),
            overview_metrics.get("price"),
        ),
        "昨日销量": first_non_empty(
            _metric_text(overview_metrics, "yday_sold_count", "yesterday_sold_count", "day1_sold_count"),
            _daily_sales_text(daily_metrics, window_days=1),
        ),
        "近7天销量": first_non_empty(
            _metric_text(overview_metrics, "day7_sold_count", "sales_7d", "day7_sales", "sold_count_7d"),
            _daily_sales_text(daily_metrics, window_days=7),
        ),
        "近90天销量": first_non_empty(
            _metric_text(overview_metrics, "day90_sold_count", "sales_90d", "day90_sales", "sold_count_90d"),
            _daily_sales_text(daily_metrics, window_days=90),
        ),
    }
    if _source_fields(source_context):
        fields["记录日期"] = first_non_empty(source_context.get("记录日期"))
    return {key: value for key, value in fields.items() if value not in ("", None, [], {})}


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


__all__ = ["run_competitor_row_refresh_flow"]
