from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerNextAction, HandlerResult
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    compact_dict,
    failed_result,
    fallback_required_result,
)


def failed_pipeline_result(
    context: HandlerContext,
    *,
    error_code: str,
    default_message: str,
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
        error_code=error_code,
        message=str(error or default_message),
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


def browser_fallback_required_pipeline_result(
    context: HandlerContext,
    *,
    flow_label: str,
    identity: Mapping[str, Any],
    source_record_id: str,
    business_key: str,
    step_timeline: list[dict[str, Any]],
    runtime_evidence: Mapping[str, Any],
    fallback_handler: str,
    fallback_payload: Mapping[str, Any],
    fallback_reason: str,
    normalized_product_result: Mapping[str, Any] | None = None,
    media_result: Mapping[str, Any] | None = None,
) -> HandlerResult:
    compact_fallback_payload = compact_dict(dict(fallback_payload))
    result = compact_dict(
        {
            "source_record_id": source_record_id,
            "business_entity_key": business_key,
            "row_status": "fallback_required",
            "fallback_required": True,
            "fallback_handler": fallback_handler,
            "fallback_reason": fallback_reason,
            "browser_fallback_payload": compact_fallback_payload,
            "product_identity": dict(identity),
            "normalized_product_result": dict(normalized_product_result or {}),
            "media_result": dict(media_result or {}),
            "step_timeline": step_timeline,
            "runtime_evidence": dict(runtime_evidence),
        }
    )
    return fallback_required_result(
        context,
        error=build_error(
            error_type="browser_fallback_required",
            error_code=f"{fallback_handler}_required",
            message=(
                f"{flow_label} requires browser fallback; workflow must dispatch "
                f"{fallback_handler} through task_execution."
            ),
            retryable=False,
            fallback_allowed=True,
            fallback_reason=fallback_reason,
            details={
                "fallback_handler": fallback_handler,
                "source_record_id": source_record_id,
                "business_entity_key": business_key,
            },
        ),
        summary={
            "source_record_id": source_record_id,
            "product_business_key": business_key,
            "row_status": "fallback_required",
            "fallback_required": True,
            "fallback_handler": fallback_handler,
            "fallback_reason": fallback_reason,
            "browser_fallback_used": True,
        },
        result=result,
        next_action=HandlerNextAction(
            type="browser_fallback",
            payload={"handler_code": fallback_handler, "payload": compact_fallback_payload},
        ),
    )
