from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.shared import coerce_bool, first_non_empty


def media_sync_payload(
    *,
    request_payload: Mapping[str, Any],
    payload: Mapping[str, Any],
    source_record_id: str,
    asset_refs: list[dict[str, Any]],
    business_key: str,
    product_id: str,
    source_context: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        **dict(request_payload),
        **dict(payload),
        "request_payload": dict(request_payload),
        "source_record_id": source_record_id,
        "asset_refs": asset_refs,
        "entity_keys": [business_key],
        "product_id": product_id,
        "source_context": dict(source_context),
        "sync_referenced_files": True,
        "require_materialized_assets": coerce_bool(
            first_non_empty(
                payload.get("require_materialized_assets"),
                request_payload.get("require_materialized_assets"),
                True,
            ),
            default=True,
        ),
    }
