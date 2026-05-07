from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.shared import first_non_empty


def fastmoss_fetch_payload(
    *,
    request_payload: Mapping[str, Any],
    payload: Mapping[str, Any],
    source_record_id: str,
    identity: Mapping[str, Any],
    source_context: Mapping[str, Any],
    overview_window_days: Any,
) -> dict[str, Any]:
    return {
        **dict(request_payload),
        **dict(payload),
        "request_payload": dict(request_payload),
        "source_record_id": source_record_id,
        "product_identity": dict(identity),
        "source_context": dict(source_context),
        "detail_level": first_non_empty(payload.get("detail_level"), "standard"),
        "fastmoss_overview_window_days": overview_window_days,
        "fastmoss_window_days": first_non_empty(
            payload.get("fastmoss_window_days"), request_payload.get("fastmoss_window_days"), 90
        ),
        "fastmoss_sku_window_days": first_non_empty(
            payload.get("fastmoss_sku_window_days"), request_payload.get("fastmoss_sku_window_days"), 28
        ),
    }
