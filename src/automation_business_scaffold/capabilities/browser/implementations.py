from __future__ import annotations

from typing import Any

from automation_business_scaffold.business.handlers._shared import (
    build_error,
    coerce_bool,
    coerce_mapping,
    coerce_mapping_list,
    failed_result,
    first_non_empty,
    normalize_product_identity,
    success_result,
)
from automation_business_scaffold.capabilities._implementations.api import _build_tiktok_normalized_product_result
from automation_business_scaffold.business.handlers.contract import HandlerContext, HandlerResult


def tiktok_product_browser_fetch_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    identity = normalize_product_identity(payload)
    if coerce_bool(payload.get("force_failure")):
        error = build_error(
            error_type="browser_failure",
            error_code="tiktok_browser_forced_failure",
            message="TikTok browser fallback was forced to fail by payload.",
            retryable=False,
            details={"product_identity": identity},
        )
        return failed_result(
            context,
            error=error,
            summary={"collection_path": "browser", "product_business_key": first_non_empty(identity.get("product_id"), identity.get("normalized_product_url"))},
        )

    normalized = coerce_mapping(payload.get("normalized_product_result"))
    if not normalized:
        browser_result = _resolve_inline_browser_payload(payload)
        normalized = _build_tiktok_normalized_product_result(
            browser_result,
            identity=identity,
            collection_path="browser",
            source_endpoint="tiktok.product.browser",
        )

    product = coerce_mapping(normalized.get("product"))
    product_id = first_non_empty(product.get("product_id"), identity.get("product_id"))
    if not product_id and not first_non_empty(product.get("product_url"), identity.get("normalized_product_url")):
        error = build_error(
            error_type="browser_failure",
            error_code="tiktok_browser_missing_identity",
            message="TikTok browser fallback did not produce a stable product identity.",
            retryable=False,
            details={"product_identity": identity},
        )
        return failed_result(
            context,
            error=error,
            summary={"collection_path": "browser"},
        )

    artifact_refs = coerce_mapping_list(payload.get("artifact_refs")) or coerce_mapping_list(normalized.get("artifact_refs"))
    summary = {
        "collection_path": "browser",
        "product_id": product_id,
        "artifact_count": len(artifact_refs),
        "media_asset_count": len(coerce_mapping_list(normalized.get("media_assets"))),
    }
    result = {
        "normalized_product_result": normalized,
        "artifact_refs": artifact_refs,
        "fallback_source_job_id": first_non_empty(payload.get("fallback_source_job_id")),
    }
    return success_result(context, summary=summary, result=result)


def _resolve_inline_browser_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("browser_result", "page_result", "mock_response", "source_payload"):
        candidate = coerce_mapping(payload.get(key))
        if candidate:
            return candidate
    source_context = coerce_mapping(payload.get("source_context"))
    for key in ("browser_result", "page_result", "product"):
        candidate = coerce_mapping(source_context.get(key))
        if candidate:
            return candidate
    return {}
