from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerResult,
)
from automation_business_scaffold.contracts.handler.dispatch import api_handler_callable
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    coerce_mapping,
    coerce_mapping_list,
    failed_result,
    fallback_required_result,
    first_non_empty,
    merge_fact_bundles,
    partial_success_result,
    success_result,
)
from automation_business_scaffold.domains.tiktok.projections.feishu_influencer_monitor_projection import (
    INFLUENCER_MONITOR_FIELD_ALLOWLIST,
)


HANDLER_CODE = "influencer_monitor_sync"

fastmoss_creator_fetch_handler = api_handler_callable("fastmoss_creator_fetch")
media_asset_sync_handler = api_handler_callable("media_asset_sync")
fact_bundle_upsert_handler = api_handler_callable("fact_bundle_upsert")
feishu_table_write_handler = api_handler_callable("feishu_table_write")


def influencer_monitor_sync_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    creator_identity = coerce_mapping(payload.get("creator_identity"))
    creator_id = _normalize_unique_id(
        creator_identity.get("creator_id"), payload.get("creator_id")
    )
    unique_id = _normalize_unique_id(creator_identity.get("unique_id"))
    uid = first_non_empty(creator_identity.get("uid"))
    if not creator_id or not unique_id or not uid or creator_id != unique_id:
        return failed_result(
            context,
            error=build_error(
                error_type="invalid_input",
                error_code="monitor_creator_identity_invalid",
                message=(
                    "influencer_monitor_sync requires creator_id equal to the "
                    "normalized FastMoss unique_id and a separate stable uid."
                ),
                retryable=False,
            ),
            summary={"creator_id": creator_id, "write_status": "failed"},
        )

    creator_fetch = fastmoss_creator_fetch_handler(
        _child_context(
            context,
            handler_code="fastmoss_creator_fetch",
            step_code="creator_fetch",
            payload={
                **payload,
                "handler_code": "fastmoss_creator_fetch",
                "creator_identity": {
                    **creator_identity,
                    "creator_id": creator_id,
                    "uid": uid,
                    "unique_id": unique_id,
                },
                "required": True,
                "detail_level": "profile_metrics_contact_goods",
                "fetch_plan": {
                    "date_type": 28,
                    "endpoints": [
                        "base_info",
                        "author_index",
                        "stat_info",
                        "contact",
                        "cargo_summary",
                        "shop_list",
                    ],
                },
                "source_context": {
                    "creator_id": creator_id,
                    "product_hits": list(payload.get("product_hits") or []),
                },
            },
        )
    )
    if creator_fetch.status == "fallback_required":
        return _propagate_fallback(
            context,
            child_result=creator_fetch,
            creator_id=creator_id,
            step_code="creator_fetch",
        )
    if creator_fetch.status == "failed":
        return failed_result(
            context,
            error=creator_fetch.error
            or build_error(
                error_type="upstream_error",
                error_code="monitor_creator_fetch_failed",
                message="FastMoss creator detail fetch failed.",
                retryable=True,
            ),
            summary={"creator_id": creator_id, "write_status": "failed"},
        )

    creator_payload = dict(creator_fetch.result)
    fact_bundle = _filter_creator_avatar_fact_media(
        merge_fact_bundles(
            coerce_mapping(creator_payload.get("fact_bundle")),
            coerce_mapping(creator_payload.get("creator_fact_bundle")),
        )
    )
    avatar_refs = _creator_avatar_refs(
        list(creator_payload.get("media_refs") or [])
    )
    durable_avatar_refs: list[dict[str, Any]] = []
    warnings = list(creator_fetch.warnings)
    if avatar_refs:
        media_result = media_asset_sync_handler(
            _child_context(
                context,
                handler_code="media_asset_sync",
                step_code="creator_avatar_sync",
                payload={
                    "request_payload": coerce_mapping(
                        payload.get("request_payload")
                    ),
                    "request_id": context.request_id,
                    "task_code": payload.get("task_code"),
                    "workflow_code": payload.get("workflow_code"),
                    "stage_code": payload.get("stage_code"),
                    "asset_refs": avatar_refs,
                    "requires_object_storage": True,
                    "require_object_storage": True,
                    "source_context": {"creator_id": creator_id},
                },
            )
        )
        warnings.extend(media_result.warnings)
        if media_result.status == "failed":
            return failed_result(
                context,
                error=media_result.error
                or build_error(
                    error_type="persistence_failure",
                    error_code="monitor_creator_avatar_sync_failed",
                    message="Creator avatar could not be materialized.",
                    retryable=True,
                ),
                summary={"creator_id": creator_id, "write_status": "failed"},
            )
        fact_bundle = merge_fact_bundles(
            fact_bundle,
            coerce_mapping(media_result.result.get("media_fact_bundle")),
        )
        durable_avatar_refs = [
            dict(item)
            for item in coerce_mapping_list(
                media_result.result.get("synced_assets")
            )
            if _is_durable_avatar(item)
        ]

    fact_result = fact_bundle_upsert_handler(
        _child_context(
            context,
            handler_code="fact_bundle_upsert",
            step_code="creator_fact_upsert",
            payload={
                "request_payload": coerce_mapping(payload.get("request_payload")),
                "request_id": context.request_id,
                "task_code": payload.get("task_code"),
                "workflow_code": payload.get("workflow_code"),
                "stage_code": payload.get("stage_code"),
                "source_job_ids": [context.job_id],
                "source_context": {
                    "creator_id": creator_id,
                    "product_ids": [
                        first_non_empty(item.get("product_id"))
                        for item in coerce_mapping_list(
                            payload.get("product_hits")
                        )
                    ],
                },
                "idempotency_context": {"creator_id": creator_id},
                "fact_bundle": fact_bundle,
                "requires_fact_db": True,
                "require_database_persistence": True,
            },
        )
    )
    warnings.extend(fact_result.warnings)
    if fact_result.status == "failed":
        return failed_result(
            context,
            error=fact_result.error
            or build_error(
                error_type="persistence_failure",
                error_code="monitor_creator_fact_upsert_failed",
                message="Creator facts could not be persisted.",
                retryable=True,
            ),
            summary={"creator_id": creator_id, "write_status": "failed"},
        )

    projection_record = {
        "creator_id": creator_id,
        "creator_unique_id": unique_id,
        "creator_fact_bundle": coerce_mapping(
            creator_payload.get("creator_fact_bundle")
        ),
        "fact_bundle": fact_bundle,
        "media_refs": durable_avatar_refs,
        "creator_run_max_sales_28d": payload.get(
            "creator_run_max_sales_28d"
        ),
        "related_product_sales_reset_days": payload.get(
            "related_product_sales_reset_days"
        ),
        "task_business_date": payload.get("task_business_date"),
        "product_hits": list(payload.get("product_hits") or []),
        "source_product_images": list(
            payload.get("source_product_images") or []
        ),
        "holidays": list(payload.get("holidays") or []),
        "cooperation_shop_names": _cooperation_shop_names(
            creator_payload,
            fact_bundle=fact_bundle,
        ),
    }
    write_payload = {
        **coerce_mapping(payload.get("request_payload")),
        "request_id": context.request_id,
        "task_code": payload.get("task_code"),
        "workflow_code": payload.get("workflow_code"),
        "stage_code": payload.get("stage_code"),
        "target_table_ref": first_non_empty(
            payload.get("target_table_ref"),
            coerce_mapping(payload.get("request_payload")).get(
                "target_table_ref"
            ),
        ),
        "mapper_code": "influencer_monitor_projection_mapper",
        "write_mode": "upsert",
        "records": [projection_record],
        "write_policy": {
            "field_allowlist": list(INFLUENCER_MONITOR_FIELD_ALLOWLIST),
            "partial_success_allowed": False,
        },
    }
    write_result = feishu_table_write_handler(
        _child_context(
            context,
            handler_code="feishu_table_write",
            step_code="monitor_target_write",
            payload=write_payload,
        )
    )
    warnings.extend(write_result.warnings)
    compact_write = _compact_write_summary(write_result)
    if write_result.status == "failed":
        return failed_result(
            context,
            error=write_result.error
            or build_error(
                error_type="upstream_error",
                error_code="monitor_target_write_failed",
                message="TK influencer monitoring target write failed.",
                retryable=True,
            ),
            summary={"creator_id": creator_id, "write_status": "failed"},
            result={
                "creator_id": creator_id,
                "write_status": "failed",
                "write_summary": compact_write,
            },
        )

    result = {
        "creator_id": creator_id,
        "write_status": (
            "unchanged" if write_result.status == "skipped" else "success"
        ),
        "creator_run_max_sales_28d": payload.get(
            "creator_run_max_sales_28d"
        ),
        "product_hit_count": len(list(payload.get("product_hits") or [])),
        "fact_summary": {
            "persistence_mode": fact_result.result.get("persistence_mode"),
            "persisted_counts": dict(
                coerce_mapping(fact_result.result.get("persisted_counts"))
            ),
        },
        "media_summary": {
            "avatar_source_count": len(avatar_refs),
            "avatar_synced_count": len(durable_avatar_refs),
        },
        "write_summary": compact_write,
    }
    summary = {
        "creator_id": creator_id,
        "write_status": result["write_status"],
        **compact_write,
    }
    if write_result.status == "partial_success":
        return partial_success_result(
            context,
            summary=summary,
            result=result,
            warnings=tuple(warnings),
        )
    return success_result(
        context,
        summary=summary,
        result=result,
        warnings=tuple(warnings),
    )


def _normalize_unique_id(*values: Any) -> str:
    return first_non_empty(*values).lstrip("@").strip()


def _propagate_fallback(
    context: HandlerContext,
    *,
    child_result: HandlerResult,
    creator_id: str,
    step_code: str,
) -> HandlerResult:
    return fallback_required_result(
        context,
        error=child_result.error
        or build_error(
            error_type="auth_failure",
            error_code="fastmoss_security_browser_fallback_required",
            message="FastMoss browser recovery is required.",
            retryable=True,
            fallback_allowed=True,
            fallback_reason="fastmoss_auth_session_recovery",
        ),
        summary={
            "creator_id": creator_id,
            "write_status": "waiting",
            "fallback_step": step_code,
        },
        result={
            **dict(child_result.result),
            "creator_id": creator_id,
            "fallback_step": step_code,
        },
        warnings=child_result.warnings,
        next_action=child_result.next_action,
    )


def _creator_avatar_refs(values: list[Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in values
        if isinstance(item, Mapping) and _is_creator_avatar(item)
    ]


def _filter_creator_avatar_fact_media(
    fact_bundle: Mapping[str, Any],
) -> dict[str, Any]:
    filtered = dict(fact_bundle)
    filtered["media_assets"] = [
        dict(item)
        for item in coerce_mapping_list(filtered.get("media_assets"))
        if _is_durable_avatar(item)
    ]
    return filtered


def _is_creator_avatar(item: Mapping[str, Any]) -> bool:
    entity_type = first_non_empty(item.get("entity_type")).lower()
    entity_key = first_non_empty(item.get("entity_key")).lower()
    role = first_non_empty(
        item.get("media_role"),
        item.get("media_type"),
    ).lower()
    return (
        entity_type == "creator" or entity_key.startswith("creator:")
    ) and role in {"creator_avatar", "avatar"}


def _is_durable_avatar(item: Mapping[str, Any]) -> bool:
    return bool(
        _is_creator_avatar(item)
        and first_non_empty(item.get("bucket"))
        and first_non_empty(item.get("object_key"))
        and first_non_empty(item.get("content_digest"))
    )


def _cooperation_shop_names(
    creator_payload: Mapping[str, Any],
    *,
    fact_bundle: Mapping[str, Any],
) -> list[str]:
    names: list[str] = []
    for source in (
        creator_payload.get("cooperation_shop_names"),
        coerce_mapping(creator_payload.get("creator_fact_bundle")).get(
            "cooperation_shops"
        ),
    ):
        values = source if isinstance(source, list) else [source]
        for value in values:
            name = first_non_empty(
                value.get("shop_name") if isinstance(value, Mapping) else value
            )
            if name and name not in names:
                names.append(name)
    for shop in coerce_mapping_list(fact_bundle.get("shops")):
        name = first_non_empty(shop.get("shop_name"), shop.get("name"))
        if name and name not in names:
            names.append(name)
    return names


def _compact_write_summary(result: HandlerResult) -> dict[str, Any]:
    payload = dict(result.result)
    created_count = 0
    updated_count = 0
    for record in coerce_mapping_list(payload.get("records")):
        if first_non_empty(record.get("status")) != "success":
            continue
        op = first_non_empty(record.get("op"))
        if op in {"append", "create", "created"}:
            created_count += 1
        if op in {"update", "updated"}:
            updated_count += 1
    skipped_count = int(payload.get("skipped_count") or 0)
    return {
        "written_count": int(payload.get("written_count") or 0),
        "failed_count": int(payload.get("failed_count") or 0),
        "created_count": created_count,
        "updated_count": updated_count,
        "unchanged_count": skipped_count
        if skipped_count
        else int(result.status == "skipped"),
    }


def _child_context(
    context: HandlerContext,
    *,
    handler_code: str,
    payload: dict[str, Any],
    step_code: str,
) -> HandlerContext:
    return HandlerContext(
        request_id=context.request_id,
        job_id=context.job_id,
        handler_code=handler_code,
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=payload,
        workflow_code=context.workflow_code,
        stage_code=context.stage_code,
        job_code=handler_code,
        item_code=step_code,
        business_key=context.business_key,
        dedupe_key=f"{context.dedupe_key}:{step_code}",
        resource_code=context.resource_code,
        worker_id=context.worker_id,
        attempt_count=context.attempt_count,
        max_attempts=context.max_attempts,
        metadata=dict(context.metadata),
    )


__all__ = [
    "HANDLER_CODE",
    "influencer_monitor_sync_handler",
]
