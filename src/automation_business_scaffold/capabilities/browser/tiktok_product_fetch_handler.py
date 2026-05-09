from __future__ import annotations

import os
from typing import Any

from automation_business_scaffold.capabilities.fact_sources.tiktok.product_normalization import (
    _build_tiktok_normalized_product_result,
)
from automation_business_scaffold.capabilities.browser.tiktok.product_page import (
    TikTokProductUnavailableError,
    fetch_tiktok_product_record_via_browser,
)
from automation_business_scaffold.contracts.handler.allowlist import BROWSER_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    coerce_bool,
    coerce_mapping,
    coerce_mapping_list,
    failed_result,
    first_non_empty,
    normalize_product_identity,
    success_result,
)

HANDLER_CODE = "tiktok_product_browser_fetch"
CONTRACT = BROWSER_HANDLER_CONTRACTS[HANDLER_CODE]


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
            summary={
                "collection_path": "browser",
                "product_business_key": first_non_empty(
                    identity.get("product_id"),
                    identity.get("normalized_product_url"),
                ),
            },
        )

    normalized = coerce_mapping(payload.get("normalized_product_result"))
    if not normalized:
        browser_result = _resolve_inline_browser_payload(payload)
        if not browser_result:
            try:
                browser_result = _fetch_browser_product_payload(payload, identity=identity)
            except TikTokProductUnavailableError as exc:
                normalized = _unavailable_product_result(identity=identity, message=str(exc))
                return success_result(
                    context,
                    summary={
                        "collection_path": "browser",
                        "availability_status": "unavailable",
                        "product_business_key": first_non_empty(
                            identity.get("product_id"),
                            identity.get("normalized_product_url"),
                        ),
                    },
                    result={
                        "normalized_product_result": normalized,
                        "availability_status": "unavailable",
                        "unavailable_message": str(exc),
                        "artifact_refs": [],
                        "fallback_source_job_id": first_non_empty(payload.get("fallback_source_job_id")),
                    },
                )
            except Exception as exc:
                error = build_error(
                    error_type="browser_failure",
                    error_code="tiktok_browser_fetch_failed",
                    message=str(exc),
                    retryable=True,
                    details={"product_identity": identity},
                )
                return failed_result(
                    context,
                    error=error,
                    summary={
                        "collection_path": "browser",
                        "product_business_key": first_non_empty(
                            identity.get("product_id"),
                            identity.get("normalized_product_url"),
                        ),
                    },
                )
        normalized = _build_tiktok_normalized_product_result(
            browser_result,
            identity=identity,
            collection_path="browser",
            source_endpoint="tiktok.product.browser",
        )

    product = coerce_mapping(normalized.get("product"))
    product_id = first_non_empty(product.get("product_id"), identity.get("product_id"))
    if not product_id and not first_non_empty(
        product.get("product_url"),
        identity.get("normalized_product_url"),
    ):
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

    artifact_refs = coerce_mapping_list(payload.get("artifact_refs")) or coerce_mapping_list(
        normalized.get("artifact_refs")
    )
    summary = {
        "collection_path": "browser",
        "product_id": product_id,
        "artifact_count": len(artifact_refs),
        "media_asset_count": len(coerce_mapping_list(normalized.get("media_assets"))),
        "slider_captcha_attempted": bool(coerce_mapping(normalized.get("slider_captcha_resolution")).get("attempted")),
        "slider_captcha_resolved": bool(coerce_mapping(normalized.get("slider_captcha_resolution")).get("resolved")),
    }
    result = {
        "normalized_product_result": normalized,
        "artifact_refs": artifact_refs,
        "slider_captcha_resolution": coerce_mapping(normalized.get("slider_captcha_resolution")),
        "slider_captcha_audit_artifact_refs": coerce_mapping_list(
            normalized.get("slider_captcha_audit_artifact_refs")
        ),
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


def _fetch_browser_product_payload(payload: dict[str, Any], *, identity: dict[str, Any]) -> dict[str, Any]:
    product_url = first_non_empty(
        payload.get("product_url"),
        payload.get("source_url"),
        payload.get("normalized_product_url"),
        identity.get("normalized_product_url"),
        identity.get("product_url"),
    )
    if not product_url:
        raise ValueError("TikTok browser fallback requires product_url.")

    timeout_ms = _coerce_int(
        first_non_empty(payload.get("browser_timeout_ms"), payload.get("tiktok_browser_timeout_ms")),
        default=30_000,
    )
    provider_name = first_non_empty(
        payload.get("tiktok_browser_provider_name"),
        payload.get("browser_provider_name"),
        os.environ.get("TIKTOK_BROWSER_PROVIDER_NAME"),
        os.environ.get("BROWSER_PROVIDER_NAME"),
    )
    profile_id = first_non_empty(
        payload.get("tiktok_browser_profile_id"),
        payload.get("browser_profile_id"),
        os.environ.get("TIKTOK_BROWSER_PROFILE_ID"),
        os.environ.get("BROWSER_PROFILE_ID"),
    )
    profile_ref = first_non_empty(
        payload.get("tiktok_browser_profile_ref"),
        payload.get("browser_profile_ref"),
        payload.get("profile_ref"),
        os.environ.get("TIKTOK_BROWSER_PROFILE_REF"),
        os.environ.get("BROWSER_PROFILE_REF"),
    )
    if provider_name and profile_id:
        profile_ref = ""
    product = fetch_tiktok_product_record_via_browser(
        product_url,
        profile_ref=profile_ref,
        workspace_id=_optional_int(
            first_non_empty(
                payload.get("tiktok_browser_workspace_id"),
                payload.get("browser_workspace_id"),
                os.environ.get("TIKTOK_BROWSER_WORKSPACE_ID"),
                os.environ.get("BROWSER_WORKSPACE_ID"),
            )
        ),
        profile_id=profile_id,
        provider_name=provider_name,
        timeout_ms=timeout_ms,
        capture_page_screenshot=coerce_bool(payload.get("capture_page_screenshot"), default=False),
        slider_captcha_audit_dir=first_non_empty(
            payload.get("tiktok_slider_captcha_audit_dir"),
            payload.get("slider_captcha_audit_dir"),
        ),
        slider_captcha_provider_config=(
            coerce_mapping(payload.get("tiktok_slider_captcha_provider_config"))
            or coerce_mapping(payload.get("slider_captcha_provider_config"))
        ),
        slider_captcha_resolver_config=(
            coerce_mapping(payload.get("tiktok_slider_captcha_resolver_config"))
            or coerce_mapping(payload.get("slider_captcha_resolver_config"))
        ),
        slider_captcha_selectors=(
            coerce_mapping(payload.get("tiktok_slider_captcha_selectors"))
            or coerce_mapping(payload.get("slider_captcha_selectors"))
        ),
        trace_id=first_non_empty(payload.get("trace_id"), payload.get("request_id")),
    )
    product_payload = product.to_dict()
    return {
        **product_payload,
        "product": product_payload,
        "sku_list": product_payload.get("skus") or [],
        "skus": product_payload.get("skus") or [],
        "gallery_images": product_payload.get("gallery_images") or [],
        "sku_images": product_payload.get("sku_images") or [],
        "slider_captcha_resolution": product_payload.get("slider_captcha_resolution") or {},
        "slider_captcha_audit_artifact_refs": product_payload.get("slider_captcha_audit_artifact_refs") or [],
    }


def _unavailable_product_result(*, identity: dict[str, Any], message: str) -> dict[str, Any]:
    raw_payload = {
        "product_id": first_non_empty(identity.get("product_id")),
        "product_url": first_non_empty(identity.get("normalized_product_url"), identity.get("product_url")),
        "availability_status": "unavailable",
        "unavailable_message": message,
    }
    normalized = _build_tiktok_normalized_product_result(
        raw_payload,
        identity=identity,
        collection_path="browser",
        source_endpoint="tiktok.product.browser",
    )
    product = coerce_mapping(normalized.get("product"))
    facts = dict(product.get("facts") or {})
    facts.update({"availability_status": "unavailable", "unavailable_message": message})
    product["facts"] = facts
    normalized["product"] = product
    normalized["logical_fields"] = {
        **dict(normalized.get("logical_fields") or {}),
        "availability_status": "unavailable",
        "unavailable_message": message,
    }
    fact_bundle = coerce_mapping(normalized.get("fact_bundle"))
    products = coerce_mapping_list(fact_bundle.get("products"))
    if products:
        products[0] = product
        fact_bundle["products"] = products
        normalized["fact_bundle"] = fact_bundle
    return normalized


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


__all__ = ["CONTRACT", "HANDLER_CODE", "tiktok_product_browser_fetch_handler"]
