from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.contracts.handler.dispatch import api_handler_callable
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    bundle_entity_keys,
    coerce_mapping,
    coerce_mapping_list,
    failed_result,
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
            result={"product_fetch_result": product_result.result},
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
    result = {
        "product_fact_bundle": coerce_mapping(product_payload.get("product_fact_bundle")),
        "normalized_creator_candidates": candidates,
        "related_creators": candidates,
        "product_hit_context": {
            "source_record_id": source_record_id,
            "product_id": product_id,
            "candidate_count": len(_related_creators(product_payload)),
            "matched_creator_count": len(candidates),
        },
        "raw_response_refs": list(product_payload.get("raw_response_refs") or []),
        "product_fetch_result": product_payload,
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

    creator_result = fastmoss_creator_fetch_handler(
        _child_context(
            context,
            handler_code="fastmoss_creator_fetch",
            payload=_creator_fetch_payload(payload, product_hits=product_hits),
            step_code="fastmoss_creator_fetch",
        )
    )
    internal_steps["creator_fetch"] = creator_result.status
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
            summary={"creator_id": creator_id, "internal_steps": internal_steps},
            result={"creator_fetch_result": creator_result.result, "product_hits": product_hits},
        )
    warnings.extend(creator_result.warnings)
    creator_payload = dict(creator_result.result)
    fact_bundle = merge_fact_bundles(
        coerce_mapping(creator_payload.get("fact_bundle")),
        coerce_mapping(creator_payload.get("creator_fact_bundle")),
    )

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
                "fact_db_url": first_non_empty(payload.get("fact_db_url"), request_payload.get("fact_db_url")),
            },
            step_code="fact_bundle_upsert",
        )
    )
    internal_steps["fact_upsert"] = fact_result.status
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
            summary={"creator_id": creator_id, "internal_steps": internal_steps},
            result={"creator_fetch_result": creator_payload, "fact_result": fact_result.result, "product_hits": product_hits},
        )
    warnings.extend(fact_result.warnings)

    media_result_payload: dict[str, Any] = {}
    media_refs = list(creator_payload.get("media_refs") or [])
    if media_refs:
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
                    "media_refs": media_refs,
                    "source_context": {"creator_id": creator_id, "product_hits": product_hits},
                },
                step_code="media_asset_sync",
            )
        )
        internal_steps["media_asset_sync"] = media_result.status
        media_result_payload = dict(media_result.result)
        warnings.extend(media_result.warnings)
    else:
        internal_steps["media_asset_sync"] = "skipped"

    write_payload = _influencer_pool_write_payload(payload, creator_payload=creator_payload, product_hits=product_hits)
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

    status_writebacks: list[dict[str, Any]] = []
    for hit in product_hits:
        if not _hit_group_terminal(hit):
            continue
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
                "write_result": dict(status_result.result),
            }
        )
    internal_steps["product_status_reconcile"] = "success" if status_writebacks else "skipped"

    result = {
        "creator_id": creator_id,
        "status": "success" if influencer_write.status in {"success", "partial_success", "skipped"} else "failed",
        "internal_steps": internal_steps,
        "creator_fact_bundle": coerce_mapping(creator_payload.get("creator_fact_bundle")),
        "fact_result": dict(fact_result.result),
        "media_asset_sync": media_result_payload,
        "influencer_pool_write": {
            "status": influencer_write.status,
            "target_table_ref": write_payload.get("target_table_ref"),
            "mapper_code": write_payload.get("mapper_code"),
            "records": list(write_payload.get("records") or []),
            "write_result": dict(influencer_write.result),
        },
        "creator_records": list(dict(influencer_write.result).get("records") or []),
        "product_hits": product_hits,
        "product_status_writebacks": status_writebacks,
        "raw_response_refs": list(creator_payload.get("raw_response_refs") or []),
    }
    summary = {
        "creator_id": creator_id,
        "product_hit_count": len(product_hits),
        "influencer_pool_write_status": influencer_write.status,
        "product_status_writeback_count": len(status_writebacks),
        "internal_steps": internal_steps,
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
    records = [_projection_record(payload, creator_payload=creator_payload, product_hit=hit) for hit in product_hits]
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
    creator_id = first_non_empty(creator_fact_bundle.get("creator_id"), creator_identity.get("creator_id"), creator_identity.get("uid"))
    source_record_id = first_non_empty(product_hit.get("source_record_id"))
    product_id = first_non_empty(product_hit.get("product_id"))
    return {
        "source_record_id": source_record_id,
        "product_id": product_id,
        "creator_id": creator_id,
        "creator_name": first_non_empty(creator_fact_bundle.get("display_name"), creator_fact_bundle.get("nickname")),
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
        },
        "matched_product_sold_count": product_hit.get("matched_product_sold_count"),
        "source_product_images": list(product_hit.get("source_product_images") or []),
        "holiday": first_non_empty(product_hit.get("holiday")),
        "product_key": first_non_empty(product_hit.get("product_key"), f"{source_record_id}:{product_id}"),
    }


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
