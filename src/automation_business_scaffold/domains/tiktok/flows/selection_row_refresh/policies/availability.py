from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.shared import coerce_mapping


def unavailable_status_field(payload: Mapping[str, Any]) -> str:
    product = coerce_mapping(payload.get("product"))
    status = str(product.get("status") or payload.get("status") or "").strip().lower()
    if status in {"off_shelf", "off_shelf_or_region_unavailable", "region_unavailable"}:
        return "已下架/区域不可售"
    return "已下架/区域不可售"
