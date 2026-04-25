from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.capabilities.channels.feishu.table_write_handler import (
    feishu_table_write_handler,
)
from automation_business_scaffold.capabilities.fact_sources.fastmoss.product_search_handler import (
    fastmoss_product_search_handler,
)
from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    coerce_mapping,
    failed_result,
    first_non_empty,
    partial_success_result,
    success_result,
)
from automation_business_scaffold.domains.tiktok.mappers.keyword_search_mapper import (
    keyword_search_parameter_mapper,
)
from automation_business_scaffold.domains.tiktok.projections.feishu_competitor_projection import (
    competitor_seed_projection_mapper,
)

HANDLER_CODE = "keyword_seed_import"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


def keyword_seed_import_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    search_request = _search_request(payload)
    seed_write = _seed_write_config(payload)
    search_context = _child_context(
        context,
        handler_code="fastmoss_product_search",
        payload=search_request,
        step_code="fastmoss_product_search",
    )
    search_result = fastmoss_product_search_handler(search_context)
    if search_result.status == "failed":
        return failed_result(
            context,
            error=search_result.error
            or build_error(
                error_type="upstream_error",
                error_code="fastmoss_product_search_failed",
                message="FastMoss product search failed.",
                retryable=True,
            ),
            summary={"search_status": search_result.status},
            result={"search_result": search_result.result},
        )

    search_payload = dict(search_result.result)
    candidates = [
        _normalize_candidate(item, search_query=str(search_request.get("search_query") or search_request.get("keyword") or ""))
        for item in search_payload.get("candidates", [])
        if isinstance(item, Mapping)
    ]

    seed_contexts: list[dict[str, Any]] = []
    seed_write_records: list[dict[str, Any]] = []
    seed_write_results: list[dict[str, Any]] = []
    written_count = 0
    skipped_count = 0
    failed_count = 0
    write_delay_seconds = _non_negative_float(payload.get("feishu_seed_write_delay_seconds"), 1.0)

    for index, candidate in enumerate(candidates, start=1):
        if index > 1 and write_delay_seconds > 0:
            time.sleep(write_delay_seconds)
        write_payload = _write_payload_for_candidate(payload, seed_write, candidate)
        seed_write_records.extend([dict(item) for item in write_payload.get("records", []) if isinstance(item, Mapping)])
        write_records = [dict(item) for item in write_payload.get("records", []) if isinstance(item, Mapping)]
        write_record = write_records[0] if write_records else {}
        projected_write_record = competitor_seed_projection_mapper(write_record, write_payload) if write_record else {}
        write_context = _child_context(
            context,
            handler_code="feishu_table_write",
            payload=write_payload,
            step_code=f"feishu_seed_write.{index}",
        )
        write_result = feishu_table_write_handler(write_context)
        result_payload = dict(write_result.result)
        records = [dict(item) for item in result_payload.get("records", []) if isinstance(item, Mapping)]
        record_result = records[0] if records else {}
        record_status = str(record_result.get("status") or write_result.status or "")
        target_record_ids = [str(item) for item in result_payload.get("target_record_ids", []) if str(item)]
        feishu_record_id = first_non_empty(record_result.get("record_id"), *(target_record_ids or []))
        source_record_id = first_non_empty(feishu_record_id, candidate.get("candidate_key"))

        if record_status == "success":
            written_count += 1
        elif record_status == "skipped" or write_result.status == "skipped":
            skipped_count += 1
        else:
            failed_count += 1

        seed_result = {
            "candidate_key": candidate["candidate_key"],
            "business_entity_key": candidate["business_entity_key"],
            "product_id": candidate.get("product_id", ""),
            "source_record_id": source_record_id,
            "status": record_status,
            "op": first_non_empty(record_result.get("op")),
            "message": first_non_empty(record_result.get("message")),
            "error_code": first_non_empty(record_result.get("error_code")),
            "error_type": first_non_empty(record_result.get("error_type")),
            "feishu_row": {
                "record_id": feishu_record_id,
                "status": record_status,
                "op": first_non_empty(record_result.get("op"), projected_write_record.get("op")),
                "fields": dict(projected_write_record.get("fields") or {}),
            },
        }
        seed_write_results.append({key: value for key, value in seed_result.items() if value not in ("", None, [], {})})
        seed_contexts.append(
            {
                **candidate,
                "source_record_id": source_record_id,
                "seed_status": record_status,
                "seed_result": result_payload,
                "feishu_row": {
                    "record_id": feishu_record_id,
                    "status": record_status,
                    "op": first_non_empty(record_result.get("op"), projected_write_record.get("op")),
                    "fields": dict(projected_write_record.get("fields") or {}),
                },
                "target_record_ids": target_record_ids,
            }
        )

    result = {
        "search_parameters": search_request,
        "normalized_candidates": candidates,
        "search_summary": dict(search_result.summary),
        "condition_summary": coerce_mapping(search_payload.get("condition_summary")),
        "pagination": coerce_mapping(search_payload.get("pagination")),
        "raw_response_ref": first_non_empty(search_payload.get("raw_response_ref")),
        "seed_write_records": seed_write_records,
        "seed_write_results": seed_write_results,
        "seed_contexts": seed_contexts,
        "written_count": written_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "target_record_ids": [
            str(item.get("source_record_id"))
            for item in seed_contexts
            if str(item.get("seed_status") or "") == "success" and str(item.get("source_record_id") or "")
        ],
        "writeback_context": {
            "seed_record_id_by_product_id": {
                str(item.get("product_id")): str(item.get("source_record_id"))
                for item in seed_contexts
                if str(item.get("seed_status") or "") == "success"
                and str(item.get("product_id") or "")
                and str(item.get("source_record_id") or "")
            }
        },
    }
    summary = {
        "candidate_count": len(candidates),
        "written_count": written_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
    }
    if failed_count and (written_count or skipped_count):
        return partial_success_result(context, summary=summary, result=result)
    if failed_count:
        return failed_result(
            context,
            error=build_error(
                error_type="upstream_error",
                error_code="keyword_seed_import_failed",
                message="Keyword seed import failed for all candidates.",
                retryable=True,
                details={"failed_count": failed_count},
            ),
            summary=summary,
            result=result,
        )
    return success_result(context, summary=summary, result=result)


def _search_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    return keyword_search_parameter_mapper(payload)


def _seed_write_config(payload: Mapping[str, Any]) -> dict[str, Any]:
    config = dict(coerce_mapping(payload.get("seed_write")))
    target_table_ref = first_non_empty(config.get("target_table_ref"), payload.get("target_table_ref"), payload.get("seed_table_ref"), payload.get("table_url"))
    return {
        **config,
        "target_table_ref": target_table_ref,
        "write_mode": first_non_empty(config.get("write_mode"), "insert_if_absent"),
        "mapper_code": first_non_empty(config.get("mapper_code"), "competitor_seed_projection_mapper"),
    }


def _write_payload_for_candidate(
    request_payload: Mapping[str, Any],
    seed_write: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        **{key: value for key, value in request_payload.items() if key in {"table_refs", "access_token", "access_token_env", "validate_schema"}},
        "stage_code": "keyword_seed_import",
        "target_table_ref": seed_write.get("target_table_ref"),
        "write_mode": seed_write.get("write_mode"),
        "mapper_code": seed_write.get("mapper_code"),
        "records": [
            {
                "business_entity_key": candidate["business_entity_key"],
                "product_id": candidate.get("product_id", ""),
                "product_url": first_non_empty(candidate.get("normalized_product_url"), candidate.get("product_url")),
                "search_query": candidate.get("search_query", ""),
                "search_rank": candidate.get("search_rank", 0),
                "candidate_key": candidate["candidate_key"],
                "source_context": dict(candidate.get("source_context") or {}),
            }
        ],
        "request_payload": dict(request_payload),
        "write_policy": {"partial_success_allowed": True},
    }


def _normalize_candidate(row: Mapping[str, Any], *, search_query: str) -> dict[str, Any]:
    product_id = first_non_empty(row.get("product_id"), row.get("goods_id"), row.get("id"))
    product_url = first_non_empty(row.get("normalized_product_url"), row.get("product_url"), row.get("url"))
    if not product_url and product_id:
        product_url = f"https://www.tiktok.com/shop/pdp/{product_id}"
    business_entity_key = _product_business_entity_key(first_non_empty(product_id, product_url, row.get("candidate_key")))
    return {
        "candidate_key": business_entity_key,
        "business_entity_key": business_entity_key,
        "product_identity": {
            "product_id": product_id,
            "product_url": product_url,
            "normalized_product_url": product_url,
        },
        "product_id": product_id,
        "product_url": product_url,
        "normalized_product_url": product_url,
        "search_query": first_non_empty(row.get("search_query"), search_query),
        "search_rank": int(row.get("rank") or row.get("search_rank") or 0),
        "source_context": dict(row),
    }


def _product_business_entity_key(value: Any) -> str:
    raw = first_non_empty(value)
    if not raw:
        return ""
    return raw if raw.startswith("product:") else f"product:{raw}"


def _non_negative_float(value: Any, default: float) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


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
        dedupe_key=f"{context.dedupe_key}:{step_code}" if context.dedupe_key else step_code,
        worker_id=context.worker_id,
        attempt_count=context.attempt_count,
        max_attempts=context.max_attempts,
        metadata=dict(context.metadata),
    )


__all__ = ["CONTRACT", "HANDLER_CODE", "keyword_seed_import_handler"]
