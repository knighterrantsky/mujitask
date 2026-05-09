from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.shared import first_non_empty


def tiktok_request_payload(
    *,
    request_payload: Mapping[str, Any],
    payload: Mapping[str, Any],
    source_record_id: str,
    identity: Mapping[str, Any],
    source_context: Mapping[str, Any],
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
        "source_context": dict(source_context),
    }
