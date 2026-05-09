from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.shared import first_non_empty


def tiktok_browser_fallback_payload(
    *,
    request_payload: Mapping[str, Any],
    payload: Mapping[str, Any],
    source_record_id: str,
    identity: Mapping[str, Any],
    source_context: Mapping[str, Any],
    fallback_source_job_id: str,
) -> dict[str, Any]:
    return {
        **dict(request_payload),
        **dict(payload),
        "request_payload": dict(request_payload),
        "source_record_id": source_record_id,
        "product_identity": dict(identity),
        "normalized_product_url": first_non_empty(
            payload.get("normalized_product_url"), identity.get("normalized_product_url")
        ),
        "product_url": first_non_empty(identity.get("normalized_product_url"), identity.get("product_url")),
        "source_context": dict(source_context),
        "fallback_source_job_id": fallback_source_job_id,
    }


def fallback_timeline_entry(*, fallback_handler: str) -> dict[str, Any]:
    return {"step": "browser_fallback", "status": "fallback_required", "fallback_handler": fallback_handler}
