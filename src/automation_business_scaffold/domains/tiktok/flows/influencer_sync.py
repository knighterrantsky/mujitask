from __future__ import annotations

import re
from collections.abc import Mapping
from time import perf_counter
from typing import Any

from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerNextAction, HandlerResult
from automation_business_scaffold.contracts.handler.dispatch import api_handler_callable
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    bundle_entity_keys,
    coerce_mapping,
    coerce_mapping_list,
    failed_result,
    fallback_required_result,
    first_non_empty,
    merge_fact_bundles,
    partial_success_result,
    success_result,
)

feishu_table_write_handler = api_handler_callable("feishu_table_write")
fastmoss_creator_fetch_handler = api_handler_callable("fastmoss_creator_fetch")
fastmoss_product_fetch_handler = api_handler_callable("fastmoss_product_fetch")
media_asset_sync_handler = api_handler_callable("media_asset_sync")
fact_bundle_upsert_handler = api_handler_callable("fact_bundle_upsert")


def run_product_creator_discovery_flow(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    product_context = _child_context(
        context,
        handler_code="fastmoss_product_fetch",
        payload=_product_fetch_payload(payload),
        step_code="fastmoss_product_fetch",
    )
    product_result = fastmoss_product_fetch_handler(product_context)
    if _requires_fastmoss_browser_recovery(product_result):
        return _fastmoss_recovery_required_or_failed(
            context,
            payload=payload,
            child_result=product_result,
            step_code="fastmoss_product_fetch",
            summary={"product_fetch_status": product_result.status},
            result={"product_fetch_summary": _compact_product_fetch_summary(product_result.result)},
        )
    if product_result.status == "failed":
        return failed_result(
            context,
            error=product_result.error
            or build_error(
                error_type="upstream_error",
                error_code="fastmoss_product_fetch_failed",
                message="FastMoss product creator discovery failed.",
                retryable=True,
            ),
            summary={"product_fetch_status": product_result.status},
            result={"product_fetch_summary": _compact_product_fetch_summary(product_result.result)},
        )

    product_payload = dict(product_result.result)
    source_context = coerce_mapping(payload.get("source_context"))
    policy = coerce_mapping(payload.get("relation_policy"))
    candidates = [
        _normalize_creator_candidate(item, source_context=source_context, relation_policy=policy)
        for item in _related_creators(product_payload)
        if isinstance(item, Mapping) and _candidate_matches_policy(item, policy)
    ]
    product_id = first_non_empty(
        source_context.get("product_id"),
        coerce_mapping(payload.get("product_identity")).get("product_id"),
        product_payload.get("product_id"),
        coerce_mapping(product_payload.get("product_fact_bundle")).get("product_id"),
    )
    source_record_id = first_non_empty(source_context.get("source_record_id"))
    compact_candidates = [_compact_discovery_creator_candidate(candidate) for candidate in candidates]
    result = {
        "product_fact_bundle": _compact_product_fact_bundle(coerce_mapping(product_payload.get("product_fact_bundle")), product_id=product_id),
        "normalized_creator_candidates": compact_candidates,
        "related_creators": compact_candidates,
        "product_hit_context": {
            "source_record_id": source_record_id,
            "product_id": product_id,
            "candidate_count": len(_related_creators(product_payload)),
            "matched_creator_count": len(candidates),
        },
        "raw_response_refs": list(product_payload.get("raw_response_refs") or []),
    }
    return success_result(
        context,
        summary={
            "source_record_id": source_record_id,
            "product_id": product_id,
            "candidate_count": len(_related_creators(product_payload)),
            "matched_creator_count": len(candidates),
        },
        result=result,
        warnings=tuple(product_result.warnings),
    )


def run_influencer_creator_sync_flow(context: HandlerContext) -> HandlerResult:
    started_at = perf_counter()
    step_timings: dict[str, dict[str, Any]] = {}
    payload = dict(context.payload)
    product_hits = coerce_mapping_list(payload.get("product_hits"))
    creator_identity = coerce_mapping(payload.get("creator_identity"))
    creator_id = first_non_empty(creator_identity.get("creator_id"), creator_identity.get("uid"), payload.get("creator_id"))
    if not creator_id:
        return failed_result(
            context,
            error=build_error(
                error_type="invalid_input",
                error_code="influencer_creator_sync_missing_creator_id",
                message="influencer_creator_sync requires creator_identity.creator_id.",
                retryable=False,
            ),
        )

    internal_steps: dict[str, str] = {}
    warnings: list[str] = []
    request_payload = coerce_mapping(payload.get("request_payload"))

    step_started_at = perf_counter()
    creator_result = fastmoss_creator_fetch_handler(
        _child_context(
            context,
            handler_code="fastmoss_creator_fetch",
            payload=_creator_fetch_payload(payload, product_hits=product_hits),
            step_code="fastmoss_creator_fetch",
        )
    )
    internal_steps["creator_fetch"] = creator_result.status
    step_timings["creator_fetch"] = _step_timing(step_started_at, status=creator_result.status)
    if _requires_fastmoss_browser_recovery(creator_result):
        return _fastmoss_recovery_required_or_failed(
            context,
            payload=payload,
            child_result=creator_result,
            step_code="fastmoss_creator_fetch",
            summary={
                "creator_id": creator_id,
                "internal_steps": internal_steps,
                "step_timings": _final_step_timings(step_timings, started_at),
            },
            result={
                "creator_fetch_result": creator_result.result,
                "product_hits": _compact_product_hits(product_hits),
                "step_timings": _final_step_timings(step_timings, started_at),
            },
        )
    if creator_result.status == "failed":
        return failed_result(
            context,
            error=creator_result.error
            or build_error(
                error_type="upstream_error",
                error_code="fastmoss_creator_fetch_failed",
                message="FastMoss creator fetch failed.",
                retryable=True,
            ),
            summary={
                "creator_id": creator_id,
                "internal_steps": internal_steps,
                "step_timings": _final_step_timings(step_timings, started_at),
            },
            result={
                "creator_fetch_result": creator_result.result,
                "product_hits": product_hits,
                "step_timings": _final_step_timings(step_timings, started_at),
            },
        )
    warnings.extend(creator_result.warnings)
    creator_payload = dict(creator_result.result)
    fact_bundle = merge_fact_bundles(
        coerce_mapping(creator_payload.get("fact_bundle")),
        coerce_mapping(creator_payload.get("creator_fact_bundle")),
    )

    media_result_payload: dict[str, Any] = {}
    media_refs = _influencer_media_refs_for_sync(list(creator_payload.get("media_refs") or []))
    fact_bundle = _filter_influencer_fact_media(fact_bundle)
    if media_refs:
        step_started_at = perf_counter()
        media_result = media_asset_sync_handler(
            _child_context(
                context,
                handler_code="media_asset_sync",
                payload={
                    "request_payload": request_payload,
                    "request_id": context.request_id,
                    "task_code": payload.get("task_code"),
                    "workflow_code": payload.get("workflow_code"),
                    "stage_code": payload.get("stage_code"),
                    "asset_refs": media_refs,
                    "requires_object_storage": True,
                    "require_object_storage": True,
                    "source_context": {"creator_id": creator_id, "product_hits": product_hits},
                },
                step_code="media_asset_sync",
            )
        )
        internal_steps["media_asset_sync"] = media_result.status
        media_result_payload = dict(media_result.result)
        warnings.extend(media_result.warnings)
        step_timings["media_asset_sync"] = _step_timing(
            step_started_at,
            status=media_result.status,
            asset_count=len(media_refs),
            synced_asset_count=len(coerce_mapping_list(media_result_payload.get("synced_assets"))),
            skipped_asset_count=len(list(creator_payload.get("media_refs") or [])) - len(media_refs),
        )
    else:
        internal_steps["media_asset_sync"] = "skipped"
        step_timings["media_asset_sync"] = _step_timing(
            perf_counter(),
            status="skipped",
            asset_count=0,
            synced_asset_count=0,
            skipped_asset_count=len(list(creator_payload.get("media_refs") or [])),
        )

    fact_bundle = merge_fact_bundles(
        fact_bundle,
        coerce_mapping(media_result_payload.get("media_fact_bundle")),
    )
    creator_payload["fact_bundle"] = fact_bundle
    creator_payload["media_refs"] = _merge_media_refs(
        media_refs,
        coerce_mapping_list(media_result_payload.get("synced_assets")),
    )
    step_started_at = perf_counter()
    fact_result = fact_bundle_upsert_handler(
        _child_context(
            context,
            handler_code="fact_bundle_upsert",
            payload={
                "request_payload": request_payload,
                "request_id": context.request_id,
                "task_code": payload.get("task_code"),
                "workflow_code": payload.get("workflow_code"),
                "stage_code": payload.get("stage_code"),
                "source_job_ids": [context.job_id],
                "source_context": _first_product_hit(product_hits),
                "idempotency_context": {"creator_id": creator_id},
                "entity_business_keys": ",".join(bundle_entity_keys(fact_bundle)),
                "fact_bundle": fact_bundle,
                "requires_fact_db": True,
                "require_database_persistence": True,
            },
            step_code="fact_bundle_upsert",
        )
    )
    internal_steps["fact_upsert"] = fact_result.status
    step_timings["fact_upsert"] = _step_timing(step_started_at, status=fact_result.status)
    if fact_result.status == "failed":
        return failed_result(
            context,
            error=fact_result.error
            or build_error(
                error_type="persistence_failure",
                error_code="fact_bundle_upsert_failed",
                message="Creator fact persistence failed.",
                retryable=True,
            ),
            summary={
                "creator_id": creator_id,
                "internal_steps": internal_steps,
                "step_timings": _final_step_timings(step_timings, started_at),
            },
            result={
                "creator_fetch_result": {"creator_id": creator_id},
                "fact_result": _compact_fact_result(fact_result.result),
                "product_hits": _compact_product_hits(product_hits),
                "step_timings": _final_step_timings(step_timings, started_at),
            },
        )
    warnings.extend(fact_result.warnings)

    write_payload = _influencer_pool_write_payload(
        payload,
        creator_payload=creator_payload,
        product_hits=product_hits,
        fact_result=fact_result.result,
    )
    step_started_at = perf_counter()
    influencer_write = feishu_table_write_handler(
        _child_context(
            context,
            handler_code="feishu_table_write",
            payload=write_payload,
            step_code="influencer_pool_write",
        )
    )
    internal_steps["influencer_pool_write"] = influencer_write.status
    warnings.extend(influencer_write.warnings)
    influencer_write_result = _compact_write_result(influencer_write.result)
    step_timings["influencer_pool_write"] = _step_timing(
        step_started_at,
        status=influencer_write.status,
        record_count=len(list(write_payload.get("records") or [])),
    )

    status_writebacks: list[dict[str, Any]] = []
    status_writeback_started_at = perf_counter()
    status_writeback_count = 0
    for hit in product_hits:
        if not _hit_group_terminal(hit):
            continue
        status_writeback_count += 1
        status_payload = _status_writeback_payload(payload, hit=hit, influencer_write=influencer_write)
        status_result = feishu_table_write_handler(
            _child_context(
                context,
                handler_code="feishu_table_write",
                payload=status_payload,
                step_code=f"product_status_writeback.{first_non_empty(hit.get('source_record_id'))}",
            )
        )
        warnings.extend(status_result.warnings)
        status_writebacks.append(
            {
                "source_record_id": first_non_empty(hit.get("source_record_id")),
                "product_id": first_non_empty(hit.get("product_id")),
                "product_group_terminal": True,
                "final_status": _final_product_status(influencer_write),
                "matched_creator_count": int(hit.get("product_group_creator_count") or 1),
                "synced_creator_count": 1 if influencer_write.status in {"success", "partial_success", "skipped"} else 0,
                "failed_creator_count": 1 if influencer_write.status == "failed" else 0,
                "status_writeback": status_result.status,
                "write_result": _compact_write_result(status_result.result),
            }
        )
    internal_steps["product_status_reconcile"] = "success" if status_writebacks else "skipped"
    step_timings["product_status_reconcile"] = _step_timing(
        status_writeback_started_at,
        status=internal_steps["product_status_reconcile"],
        writeback_count=status_writeback_count,
    )
    final_step_timings = _final_step_timings(step_timings, started_at)

    result = {
        "creator_id": creator_id,
        "status": "success" if influencer_write.status in {"success", "partial_success", "skipped"} else "failed",
        "internal_steps": internal_steps,
        "step_timings": final_step_timings,
        "creator_fact_bundle": _compact_creator_fact_bundle(coerce_mapping(creator_payload.get("creator_fact_bundle"))),
        "fact_result": _compact_fact_result(fact_result.result),
        "media_asset_sync": _compact_media_result(media_result_payload),
        "influencer_pool_write": {
            "status": influencer_write.status,
            "target_table_ref": write_payload.get("target_table_ref"),
            "mapper_code": write_payload.get("mapper_code"),
            "write_result": influencer_write_result,
        },
        "product_hits": _compact_product_hits(product_hits),
        "product_status_writebacks": status_writebacks,
        "raw_response_refs": list(creator_payload.get("raw_response_refs") or []),
    }
    summary = {
        "creator_id": creator_id,
        "product_hit_count": len(product_hits),
        "influencer_pool_write_status": influencer_write.status,
        "influencer_write_written_count": influencer_write_result["written_count"],
        "influencer_write_failed_count": influencer_write_result["failed_count"],
        "influencer_write_created_count": _write_result_op_count(influencer_write_result, ("append", "create", "created")),
        "influencer_write_updated_count": _write_result_op_count(influencer_write_result, ("update", "updated")),
        "product_status_writeback_count": len(status_writebacks),
        "internal_steps": internal_steps,
        "step_timings": final_step_timings,
    }
    if influencer_write.status == "failed":
        return failed_result(
            context,
            error=influencer_write.error
            or build_error(
                error_type="upstream_error",
                error_code="influencer_pool_write_failed",
                message="Influencer pool write failed.",
                retryable=True,
            ),
            summary=summary,
            result=result,
            warnings=warnings,
        )
    if influencer_write.status == "partial_success":
        return partial_success_result(context, summary=summary, result=result, warnings=warnings)
    return success_result(context, summary=summary, result=result, warnings=warnings)


def _step_timing(started_at: float, *, status: str, **metadata: Any) -> dict[str, Any]:
    timing = {
        "status": status,
        "duration_seconds": round(max(perf_counter() - started_at, 0.0), 3),
    }
    for key, value in metadata.items():
        if value in (None, ""):
            continue
        timing[key] = value
    return timing


def _final_step_timings(step_timings: Mapping[str, dict[str, Any]], started_at: float) -> dict[str, Any]:
    return {
        **{key: dict(value) for key, value in step_timings.items()},
        "total": {
            "status": "observed",
            "duration_seconds": round(max(perf_counter() - started_at, 0.0), 3),
        },
    }


def _requires_fastmoss_browser_recovery(result: HandlerResult) -> bool:
    if result.status == "fallback_required":
        return True
    if result.status != "failed" or result.error is None:
        return False
    return result.error.error_code in {
        "fastmoss_auth_required",
        "fastmoss_auth_session_recovery_required",
        "fastmoss_session_conflict_or_external_login",
        "fastmoss_security_verification_required",
    }


def _fastmoss_recovery_required_or_failed(
    context: HandlerContext,
    *,
    payload: Mapping[str, Any],
    child_result: HandlerResult,
    step_code: str,
    summary: dict[str, Any],
    result: dict[str, Any],
) -> HandlerResult:
    if int(payload.get("fastmoss_security_browser_fallback_attempt") or 0) > 0:
        return failed_result(
            context,
            error=build_error(
                error_type="auth_failure",
                error_code="fastmoss_browser_fallback_retry_exhausted",
                message="FastMoss still requires auth or security recovery after browser fallback.",
                retryable=False,
                details={"step_code": step_code, "child_error_code": _child_error_code(child_result)},
            ),
            summary={**summary, "fallback_source_step": step_code},
            result=result,
        )
    fallback_payload = _fastmoss_browser_recovery_payload(
        context,
        parent_payload=payload,
        child_result=child_result,
        step_code=step_code,
    )
    error = child_result.error or build_error(
        error_type="auth_failure",
        error_code="fastmoss_auth_session_recovery_required",
        message="FastMoss auth or security recovery is required.",
        retryable=False,
        fallback_allowed=True,
        fallback_reason=str(fallback_payload.get("fallback_reason") or "fastmoss_auth_session_recovery"),
    )
    next_action = child_result.next_action
    if next_action.type == "none":
        next_action = HandlerNextAction(type="browser_fallback", payload=fallback_payload)
    return fallback_required_result(
        context,
        error=error,
        summary={
            **summary,
            "fallback_required": True,
            "fallback_source_step": step_code,
            "fallback_reason": str(fallback_payload.get("fallback_reason") or ""),
        },
        result={**result, **fallback_payload},
        next_action=next_action,
    )


def _fastmoss_browser_recovery_payload(
    context: HandlerContext,
    *,
    parent_payload: Mapping[str, Any],
    child_result: HandlerResult,
    step_code: str,
) -> dict[str, Any]:
    payload = dict(coerce_mapping(child_result.result))
    payload.setdefault("fallback_required", True)
    payload.setdefault("fallback_reason", _fastmoss_fallback_reason(child_result))
    payload.setdefault("source_handler_code", child_result.handler_code)
    payload["retry_handler_code"] = context.handler_code
    payload["source_step_code"] = step_code
    payload.setdefault("operation", child_result.handler_code)
    if not coerce_mapping(payload.get("verification_request")):
        verification_request = _fallback_verification_request_from_payload(parent_payload, child_result.handler_code)
        if verification_request:
            payload["verification_request"] = verification_request
    return payload


def _fastmoss_fallback_reason(result: HandlerResult) -> str:
    if result.error and result.error.fallback_reason:
        return result.error.fallback_reason
    if result.error and result.error.error_code in {
        "fastmoss_auth_required",
        "fastmoss_auth_session_recovery_required",
        "fastmoss_session_conflict_or_external_login",
    }:
        return "fastmoss_auth_session_recovery"
    return "fastmoss_api_security_verification"


def _child_error_code(result: HandlerResult) -> str:
    return result.error.error_code if result.error is not None else ""


def _fallback_verification_request_from_payload(parent_payload: Mapping[str, Any], handler_code: str) -> dict[str, Any]:
    if handler_code == "fastmoss_product_fetch":
        product_identity = coerce_mapping(parent_payload.get("product_identity"))
        source_context = coerce_mapping(parent_payload.get("source_context"))
        product_id = first_non_empty(product_identity.get("product_id"), source_context.get("product_id"))
        return {
            "method": "GET",
            "path": "/api/goods/v3/base",
            "params": {"product_id": product_id} if product_id else {},
            "region": first_non_empty(parent_payload.get("region"), "US"),
            "stage": "product.auth_recovery",
        }
    if handler_code == "fastmoss_creator_fetch":
        creator_identity = coerce_mapping(parent_payload.get("creator_identity"))
        uid = first_non_empty(
            creator_identity.get("uid"),
            creator_identity.get("creator_id"),
            creator_identity.get("unique_id"),
        )
        return {
            "method": "GET",
            "path": "/api/author/v3/detail/baseInfo",
            "params": {"uid": uid} if uid else {},
            "region": first_non_empty(parent_payload.get("region"), "US"),
            "stage": "creator.auth_recovery",
        }
    return {}


def _compact_creator_fact_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    metrics = coerce_mapping(bundle.get("metrics"))
    contact = coerce_mapping(bundle.get("contact"))
    return {
        "creator_id": first_non_empty(bundle.get("creator_id")),
        "uid": first_non_empty(bundle.get("uid")),
        "unique_id": first_non_empty(bundle.get("unique_id")),
        "display_name": first_non_empty(bundle.get("display_name"), bundle.get("nickname")),
        "nickname": first_non_empty(bundle.get("nickname")),
        "profile_url": first_non_empty(bundle.get("profile_url")),
        "avatar_url": first_non_empty(bundle.get("avatar_url")),
        "metrics": {
            key: metrics.get(key)
            for key in (
                "follower_count",
                "fans_count",
                "aweme_28d_count",
                "aweme_28_count",
                "video_count",
                "video_sale_amount",
                "video_gmv",
                "live_sale_amount",
                "live_gmv",
            )
            if metrics.get(key) not in (None, "")
        },
        "contact": {
            key: contact.get(key)
            for key in ("normalized_text",)
            if contact.get(key) not in (None, "")
        },
    }


def _compact_fact_result(result: Mapping[str, Any]) -> dict[str, Any]:
    persisted_counts = coerce_mapping(result.get("persisted_counts"))
    return {
        "persisted_counts": dict(persisted_counts),
        "upserted_entity_count": len(list(result.get("upserted_entities") or [])),
        "upserted_relation_count": len(list(result.get("upserted_relations") or [])),
        "observation_ref_count": len(list(result.get("observation_refs") or [])),
        "persistence_mode": first_non_empty(result.get("persistence_mode")),
    }


def _compact_product_fact_bundle(bundle: Mapping[str, Any], *, product_id: str) -> dict[str, Any]:
    products = coerce_mapping_list(bundle.get("products"))
    first_product = products[0] if products else {}
    return {
        "product_id": first_non_empty(bundle.get("product_id"), first_product.get("product_id"), product_id),
        "product_key": first_non_empty(bundle.get("product_key"), first_product.get("product_key")),
        "entity_count": len(bundle_entity_keys(bundle)),
        "raw_response_count": len(coerce_mapping_list(bundle.get("raw_api_responses"))),
        "media_asset_count": len(coerce_mapping_list(bundle.get("media_assets"))),
    }


def _compact_product_fetch_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    product_fact_bundle = coerce_mapping(result.get("product_fact_bundle"))
    related_creators = _related_creators(result)
    return {
        "product_fact_bundle": _compact_product_fact_bundle(product_fact_bundle, product_id=""),
        "related_creator_count": len(related_creators),
        "raw_response_refs": list(result.get("raw_response_refs") or []),
    }


def _compact_discovery_creator_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    creator_identity = coerce_mapping(candidate.get("creator_identity"))
    metrics = coerce_mapping(candidate.get("metrics"))
    source_context = coerce_mapping(candidate.get("source_context"))
    creator_id = first_non_empty(
        creator_identity.get("creator_id"),
        candidate.get("creator_id"),
        candidate.get("influencer_id"),
        creator_identity.get("unique_id"),
        creator_identity.get("uid"),
    )
    return {
        "creator_id": creator_id,
        "uid": first_non_empty(creator_identity.get("uid"), candidate.get("uid"), creator_id),
        "unique_id": first_non_empty(creator_identity.get("unique_id"), candidate.get("unique_id")),
        "nickname": first_non_empty(candidate.get("nickname"), candidate.get("display_name"), creator_identity.get("nickname")),
        "display_name": first_non_empty(candidate.get("display_name"), candidate.get("nickname"), creator_identity.get("nickname")),
        "profile_url": first_non_empty(creator_identity.get("profile_url"), candidate.get("profile_url")),
        "creator_identity": {
            "creator_id": creator_id,
            "uid": first_non_empty(creator_identity.get("uid"), candidate.get("uid"), creator_id),
            "unique_id": first_non_empty(creator_identity.get("unique_id"), candidate.get("unique_id")),
            "nickname": first_non_empty(candidate.get("nickname"), candidate.get("display_name"), creator_identity.get("nickname")),
            "profile_url": first_non_empty(creator_identity.get("profile_url"), candidate.get("profile_url")),
        },
        "metrics": {
            key: metrics.get(key)
            for key in ("sold_count", "sales_count", "follower_count", "fans_count")
            if metrics.get(key) not in (None, "")
        },
        "matched_conditions": dict(coerce_mapping(candidate.get("matched_conditions"))),
        "source_context": {
            "source_record_id": first_non_empty(source_context.get("source_record_id")),
            "product_id": first_non_empty(source_context.get("product_id")),
            "product_key": first_non_empty(source_context.get("product_key")),
        },
    }


def _compact_media_result(result: Mapping[str, Any]) -> dict[str, Any]:
    synced_assets = coerce_mapping_list(result.get("synced_assets"))
    artifact_refs = coerce_mapping_list(result.get("artifact_refs"))
    durable_assets = [
        asset
        for asset in synced_assets
        if first_non_empty(asset.get("bucket"))
        and first_non_empty(asset.get("object_key"))
        and re.fullmatch(
            r"[0-9a-f]{64}",
            first_non_empty(asset.get("content_digest")),
        )
    ]
    return {
        "synced_count": len(synced_assets),
        "artifact_count": len(artifact_refs),
        "synced_assets": [
            {
                "entity_type": first_non_empty(asset.get("entity_type")),
                "entity_external_id": first_non_empty(asset.get("entity_external_id")),
                "media_role": first_non_empty(asset.get("media_role"), asset.get("media_type")),
                "sync_state": first_non_empty(asset.get("sync_state")),
                "bucket": first_non_empty(asset.get("bucket")),
                "object_key": first_non_empty(asset.get("object_key")),
                "content_digest": first_non_empty(asset.get("content_digest")),
            }
            for asset in durable_assets
        ],
        "artifact_refs": [
            {
                "artifact_id": first_non_empty(ref.get("artifact_id")),
                "bucket": first_non_empty(ref.get("bucket")),
                "object_key": first_non_empty(ref.get("object_key")),
                "content_digest": first_non_empty(
                    ref.get("content_digest"),
                    coerce_mapping(ref.get("metadata")).get("content_digest"),
                ),
            }
            for ref in artifact_refs
        ],
    }


def _compact_write_result(result: Mapping[str, Any]) -> dict[str, Any]:
    records = [
        {
            "business_entity_key": first_non_empty(record.get("business_entity_key")),
            "record_id": first_non_empty(record.get("record_id")),
            "op": first_non_empty(record.get("op")),
            "status": first_non_empty(record.get("status")),
            "error_code": first_non_empty(record.get("error_code")),
        }
        for record in coerce_mapping_list(result.get("records"))
    ]
    return {
        "written_count": int(result.get("written_count") or 0),
        "skipped_count": int(result.get("skipped_count") or 0),
        "failed_count": int(result.get("failed_count") or 0),
        "target_record_ids": list(result.get("target_record_ids") or []),
        "records": records,
    }


def _write_result_op_count(result: Mapping[str, Any], ops: tuple[str, ...]) -> int:
    allowed = {first_non_empty(op) for op in ops if first_non_empty(op)}
    count = 0
    for record in coerce_mapping_list(result.get("records")):
        if first_non_empty(record.get("status")) != "success":
            continue
        if first_non_empty(record.get("op")) in allowed:
            count += 1
    return count


def _compact_product_hits(product_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for hit in product_hits:
        compacted.append(
            {
                "source_record_id": first_non_empty(hit.get("source_record_id")),
                "product_id": first_non_empty(hit.get("product_id")),
                "product_key": first_non_empty(hit.get("product_key")),
                "holiday": first_non_empty(hit.get("holiday")),
                "matched_product_sold_count": hit.get("matched_product_sold_count"),
                "product_group_creator_count": hit.get("product_group_creator_count"),
                "product_group_terminal": bool(hit.get("product_group_terminal")) if "product_group_terminal" in hit else None,
                "source_product_images": _compact_attachment_refs(list(hit.get("source_product_images") or [])),
            }
        )
    return compacted


def _compact_attachment_refs(items: list[Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        refs.append(
            {
                "file_token": first_non_empty(item.get("file_token")),
                "name": first_non_empty(item.get("name")),
                "url": first_non_empty(item.get("url")),
                "tmp_url": first_non_empty(item.get("tmp_url")),
            }
        )
    return refs


def _influencer_media_refs_for_sync(media_refs: list[Any]) -> list[dict[str, Any]]:
    return [
        dict(ref)
        for ref in media_refs
        if isinstance(ref, Mapping) and _is_creator_avatar_media(ref)
    ]


def _filter_influencer_fact_media(fact_bundle: Mapping[str, Any]) -> dict[str, Any]:
    filtered = dict(fact_bundle)
    filtered["media_assets"] = [
        dict(asset)
        for asset in coerce_mapping_list(filtered.get("media_assets"))
        if _is_creator_avatar_media(asset)
    ]
    return filtered


def _is_creator_avatar_media(media: Mapping[str, Any]) -> bool:
    entity_type = first_non_empty(media.get("entity_type")).lower()
    role = first_non_empty(media.get("media_role"), media.get("media_type")).lower()
    return entity_type == "creator" and role in {"creator_avatar", "avatar"}


def _product_fetch_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    plan = coerce_mapping(payload.get("discovery_plan"))
    return {
        **dict(payload),
        "handler_code": "fastmoss_product_fetch",
        "detail_level": first_non_empty(plan.get("detail_level"), payload.get("detail_level"), "related_creators"),
    }


def _related_creators(product_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return coerce_mapping_list(
        product_payload.get("related_creators")
        or product_payload.get("normalized_creator_candidates")
        or coerce_mapping(product_payload.get("result")).get("related_creators")
    )


def _candidate_matches_policy(candidate: Mapping[str, Any], policy: Mapping[str, Any]) -> bool:
    sold_min = int(policy.get("creator_sold_count_min") or policy.get("min_sold_count") or 50)
    follower_min = int(policy.get("creator_follower_count_min") or policy.get("min_follower_count") or 5000)
    metrics = coerce_mapping(candidate.get("metrics"))
    sold_count = _int_value(candidate.get("sold_count"), metrics.get("sold_count"), metrics.get("sales_count"))
    follower_count = _int_value(candidate.get("follower_count"), metrics.get("follower_count"), metrics.get("fans_count"))
    return sold_count > sold_min and follower_count > follower_min


def _normalize_creator_candidate(
    candidate: Mapping[str, Any],
    *,
    source_context: Mapping[str, Any],
    relation_policy: Mapping[str, Any],
) -> dict[str, Any]:
    metrics = coerce_mapping(candidate.get("metrics"))
    creator_identity = coerce_mapping(candidate.get("creator_identity"))
    creator_id = first_non_empty(
        creator_identity.get("creator_id"),
        candidate.get("creator_id"),
        candidate.get("influencer_id"),
        creator_identity.get("uid"),
    )
    sold_count = _int_value(candidate.get("sold_count"), metrics.get("sold_count"), metrics.get("sales_count"))
    follower_count = _int_value(candidate.get("follower_count"), metrics.get("follower_count"), metrics.get("fans_count"))
    return {
        **dict(candidate),
        "creator_id": creator_id,
        "creator_identity": {
            **creator_identity,
            "creator_id": creator_id,
            "uid": first_non_empty(creator_identity.get("uid"), candidate.get("uid"), creator_id),
            "profile_url": first_non_empty(creator_identity.get("profile_url"), candidate.get("profile_url")),
        },
        "display_name": first_non_empty(candidate.get("display_name"), candidate.get("nickname"), candidate.get("creator_name")),
        "metrics": {**metrics, "sold_count": sold_count, "follower_count": follower_count},
        "matched_conditions": {
            "creator_sold_count_min": sold_count > int(relation_policy.get("creator_sold_count_min") or 50),
            "creator_follower_count_min": follower_count > int(relation_policy.get("creator_follower_count_min") or 5000),
        },
        "source_context": {
            **dict(source_context),
            "source_record_id": first_non_empty(source_context.get("source_record_id")),
            "product_id": first_non_empty(source_context.get("product_id")),
        },
    }


def _creator_fetch_payload(payload: Mapping[str, Any], *, product_hits: list[dict[str, Any]]) -> dict[str, Any]:
    sync_plan = coerce_mapping(payload.get("sync_plan"))
    fetch_plan = {**coerce_mapping(payload.get("fetch_plan")), **coerce_mapping(sync_plan.get("creator_fetch"))}
    first_hit = _first_product_hit(product_hits)
    return {
        **dict(payload),
        "handler_code": "fastmoss_creator_fetch",
        "detail_level": first_non_empty(fetch_plan.get("detail_level"), payload.get("detail_level"), "profile_metrics_contact_goods"),
        "fetch_plan": {
            "date_type": fetch_plan.get("date_type", 28),
            "endpoints": list(
                fetch_plan.get("endpoints")
                or ["base_info", "author_index", "stat_info", "contact", "cargo_summary", "shop_list", "goods_list"]
            ),
        },
        "source_context": {
            **first_hit,
            "product_hits": product_hits,
        },
    }


def _influencer_pool_write_payload(
    payload: Mapping[str, Any],
    *,
    creator_payload: Mapping[str, Any],
    product_hits: list[dict[str, Any]],
    fact_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    request_payload = coerce_mapping(payload.get("request_payload"))
    target_table_ref = first_non_empty(
        payload.get("influencer_pool_table_ref"),
        payload.get("target_table_ref"),
        request_payload.get("influencer_pool_table_ref"),
        request_payload.get("target_table_ref"),
        request_payload.get("source_table_ref"),
        payload.get("source_table_ref"),
    )
    merged_hit = _merged_product_hit_for_influencer_write(product_hits, fact_result=fact_result or {})
    records = (
        [_projection_record(payload, creator_payload=creator_payload, product_hit=merged_hit)]
        if merged_hit
        else []
    )
    return {
        **request_payload,
        "request_id": payload.get("request_id"),
        "task_code": payload.get("task_code"),
        "workflow_code": payload.get("workflow_code"),
        "stage_code": payload.get("stage_code"),
        "target_table_ref": target_table_ref,
        "mapper_code": "influencer_pool_projection_mapper",
        "write_mode": "upsert",
        "records": records,
    }


def _projection_record(
    payload: Mapping[str, Any],
    *,
    creator_payload: Mapping[str, Any],
    product_hit: Mapping[str, Any],
) -> dict[str, Any]:
    creator_fact_bundle = coerce_mapping(creator_payload.get("creator_fact_bundle"))
    creator_identity = coerce_mapping(payload.get("creator_identity"))
    creator_id = first_non_empty(
        creator_fact_bundle.get("creator_id"),
        creator_identity.get("creator_id"),
        creator_identity.get("uid"),
    )
    source_record_id = first_non_empty(product_hit.get("source_record_id"))
    product_id = first_non_empty(product_hit.get("product_id"))
    holiday_value = product_hit.get("holiday")
    return {
        "source_record_id": source_record_id,
        "product_id": product_id,
        "creator_id": creator_id,
        "creator_name": first_non_empty(
            creator_fact_bundle.get("display_name"),
            creator_fact_bundle.get("nickname"),
        ),
        "creator_fact_bundle": creator_fact_bundle,
        "fact_bundle": coerce_mapping(creator_payload.get("fact_bundle")),
        "entities": coerce_mapping(creator_payload.get("entities")),
        "relations": list(creator_payload.get("relations") or []),
        "observations": list(creator_payload.get("observations") or []),
        "media_refs": list(creator_payload.get("media_refs") or []),
        "product_relations": list(creator_payload.get("product_relations") or []),
        "source_context": {
            **dict(product_hit),
            "source_record_id": source_record_id,
            "product_id": product_id,
            "matched_product_sold_count": product_hit.get("matched_product_sold_count"),
            "matched_product_sold_delta": product_hit.get("matched_product_sold_delta"),
        },
        "matched_product_sold_count": product_hit.get("matched_product_sold_count"),
        "matched_product_sold_delta": product_hit.get("matched_product_sold_delta"),
        "source_product_images": list(product_hit.get("source_product_images") or []),
        "holiday": holiday_value if isinstance(holiday_value, list) else first_non_empty(holiday_value),
        "product_key": first_non_empty(product_hit.get("product_key"), f"{source_record_id}:{product_id}"),
    }


def _merged_product_hit_for_influencer_write(
    product_hits: list[dict[str, Any]],
    *,
    fact_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    unique_hits = _unique_product_hits(product_hits)
    if not unique_hits:
        return {}
    merged = dict(unique_hits[0])
    source_product_images = _source_product_images_for_unique_hits(unique_hits)
    holidays = _unique_text_values(hit.get("holiday") for hit in unique_hits)
    product_ids = _unique_text_values(hit.get("product_id") for hit in unique_hits)
    source_record_ids = _unique_text_values(hit.get("source_record_id") for hit in unique_hits)
    sales_hits = _sales_delta_product_hits(unique_hits, fact_result=fact_result or {})
    sales_total = sum(_numeric_value(hit.get("matched_product_sold_count")) or 0.0 for hit in unique_hits)
    sales_delta = sum(_numeric_value(hit.get("matched_product_sold_count")) or 0.0 for hit in sales_hits)
    if source_product_images:
        merged["source_product_images"] = source_product_images
    if holidays:
        merged["holiday"] = holidays
    merged["matched_product_sold_count"] = sales_total
    merged["matched_product_sold_delta"] = sales_delta
    if product_ids:
        merged["product_ids"] = product_ids
    if source_record_ids:
        merged["source_record_ids"] = source_record_ids
    merged["product_hits"] = unique_hits
    return merged


def _sales_delta_product_hits(
    product_hits: list[dict[str, Any]],
    *,
    fact_result: Mapping[str, Any],
) -> list[dict[str, Any]]:
    created_product_ids = {
        first_non_empty(relation.get("product_id"))
        for relation in coerce_mapping_list(fact_result.get("created_creator_product_relations"))
    }
    if not fact_result or not created_product_ids:
        return product_hits if not fact_result else []
    return [
        hit
        for hit in product_hits
        if first_non_empty(hit.get("product_id")) in created_product_ids
    ]


def _unique_product_hits(product_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique_hits: list[dict[str, Any]] = []
    hits_by_key: dict[str, dict[str, Any]] = {}
    for index, hit in enumerate(product_hits):
        product_key = _product_hit_dedupe_key(hit, fallback=f"index:{index}")
        if product_key in hits_by_key:
            existing_hit = hits_by_key[product_key]
            if not list(existing_hit.get("source_product_images") or []) and list(
                hit.get("source_product_images") or []
            ):
                existing_hit["source_product_images"] = list(hit.get("source_product_images") or [])
            continue
        item = dict(hit)
        hits_by_key[product_key] = item
        unique_hits.append(item)
    return unique_hits


def _product_hit_dedupe_key(hit: Mapping[str, Any], *, fallback: str) -> str:
    return first_non_empty(
        hit.get("product_id"),
        hit.get("product_key"),
        hit.get("source_record_id"),
        fallback,
    )


def _source_product_images_for_unique_hits(product_hits: list[dict[str, Any]]) -> list[Any]:
    refs: list[Any] = []
    seen: set[str] = set()
    for hit in product_hits:
        for image in list(hit.get("source_product_images") or []):
            key = _source_product_image_key(image)
            if not key or key in seen:
                continue
            seen.add(key)
            refs.append(dict(image) if isinstance(image, Mapping) else image)
    return refs


def _source_product_image_key(image: Any) -> str:
    if isinstance(image, Mapping):
        return first_non_empty(
            image.get("file_token"),
            image.get("url"),
            image.get("source_url"),
            image.get("tmp_url"),
            image.get("download_url"),
            image.get("local_path"),
            image.get("source_path"),
            image.get("path"),
            image.get("object_key"),
        )
    return first_non_empty(image)


def _unique_text_values(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        items = value if isinstance(value, list) else [value]
        for item in items:
            text = first_non_empty(item)
            if text and text not in result:
                result.append(text)
    return result


def _numeric_value(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = first_non_empty(value).replace(",", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def _status_writeback_payload(payload: Mapping[str, Any], *, hit: Mapping[str, Any], influencer_write: HandlerResult) -> dict[str, Any]:
    request_payload = coerce_mapping(payload.get("request_payload"))
    target_table_ref = first_non_empty(
        coerce_mapping(hit.get("source_status_writeback")).get("target_table_ref"),
        payload.get("competitor_status_table_ref"),
        request_payload.get("competitor_status_table_ref"),
        request_payload.get("source_table_ref"),
        request_payload.get("source_table_url"),
        request_payload.get("table_url"),
    )
    source_record_id = first_non_empty(hit.get("source_record_id"), coerce_mapping(hit.get("source_status_writeback")).get("record_id"))
    product_id = first_non_empty(hit.get("product_id"))
    final_status = _final_product_status(influencer_write)
    return {
        **request_payload,
        "request_id": payload.get("request_id"),
        "task_code": payload.get("task_code"),
        "workflow_code": payload.get("workflow_code"),
        "stage_code": payload.get("stage_code"),
        "target_table_ref": target_table_ref,
        "mapper_code": "competitor_influencer_status_projection_mapper",
        "write_mode": "upsert",
        "records": [
            {
                "source_record_id": source_record_id,
                "product_id": product_id,
                "product_key": first_non_empty(hit.get("product_key"), f"{source_record_id}:{product_id}"),
                "influencer_sync_status": "success" if final_status == "已完成" else "failed",
                "creator_candidate_count": int(hit.get("product_group_creator_count") or 1),
                "creator_detail_success_count": 1 if final_status == "已完成" else 0,
                "creator_detail_failed_count": 0 if final_status == "已完成" else 1,
                "influencer_write_success_count": 1 if final_status == "已完成" else 0,
                "warnings": [] if final_status == "已完成" else ["influencer_creator_sync_failed"],
            }
        ],
    }


def _final_product_status(influencer_write: HandlerResult) -> str:
    return "已完成" if influencer_write.status in {"success", "partial_success", "skipped"} else "失败重试"


def _merge_media_refs(existing_refs: list[Any], synced_assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = [
        dict(item)
        for item in existing_refs
        if isinstance(item, Mapping)
        and first_non_empty(item.get("bucket"))
        and first_non_empty(item.get("object_key"))
        and re.fullmatch(
            r"[0-9a-f]{64}",
            first_non_empty(item.get("content_digest")),
        )
    ]
    seen = {
        _media_ref_key(ref)
        for ref in refs
        if _media_ref_key(ref)
    }
    for asset in synced_assets:
        if (
            not first_non_empty(asset.get("bucket"))
            or not first_non_empty(asset.get("object_key"))
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                first_non_empty(asset.get("content_digest")),
            )
        ):
            continue
        ref = {
            "entity_key": asset.get("entity_key"),
            "entity_type": asset.get("entity_type"),
            "entity_external_id": asset.get("entity_external_id"),
            "media_role": asset.get("media_role"),
            "media_type": asset.get("media_role"),
            "source_url": asset.get("source_url"),
            "bucket": asset.get("bucket"),
            "object_key": asset.get("object_key"),
            "content_digest": asset.get("content_digest"),
            "file_name": asset.get("file_name"),
            "mime_type": asset.get("mime_type"),
            "size_bytes": asset.get("size_bytes"),
            "source_platform": asset.get("source_platform"),
            "metadata": coerce_mapping(asset.get("metadata")),
        }
        key = _media_ref_key(ref)
        if not key or key in seen:
            continue
        seen.add(key)
        refs.append(ref)
    return refs


def _media_ref_key(ref: Mapping[str, Any]) -> str:
    return ":".join(
        [
            first_non_empty(ref.get("entity_type")),
            first_non_empty(ref.get("entity_external_id")),
            first_non_empty(ref.get("bucket")),
            first_non_empty(ref.get("object_key")),
            first_non_empty(ref.get("content_digest")),
        ]
    )


def _hit_group_terminal(hit: Mapping[str, Any]) -> bool:
    if "product_group_terminal" in hit:
        return bool(hit.get("product_group_terminal"))
    return int(hit.get("product_group_creator_count") or 1) <= 1


def _first_product_hit(product_hits: list[dict[str, Any]]) -> dict[str, Any]:
    return dict(product_hits[0]) if product_hits else {}


def _int_value(*values: Any) -> int:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return int(float(str(value).replace(",", "").strip()))
        except (TypeError, ValueError):
            continue
    return 0


def _child_context(context: HandlerContext, *, handler_code: str, payload: Mapping[str, Any], step_code: str) -> HandlerContext:
    return HandlerContext(
        request_id=context.request_id,
        job_id=f"{context.job_id}:{step_code}",
        handler_code=handler_code,
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=dict(payload),
        workflow_code=context.workflow_code,
        stage_code=context.stage_code,
        job_code=handler_code,
        business_key=context.business_key,
        dedupe_key=f"{context.dedupe_key}:{step_code}" if context.dedupe_key else "",
        resource_code=context.resource_code,
        worker_id=context.worker_id,
        attempt_count=context.attempt_count,
        max_attempts=context.max_attempts,
        metadata=dict(context.metadata),
    )


__all__ = [
    "run_influencer_creator_sync_flow",
    "run_product_creator_discovery_flow",
]
