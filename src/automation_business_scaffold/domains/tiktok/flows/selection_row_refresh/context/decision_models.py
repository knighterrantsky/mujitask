from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.shared import coerce_bool, coerce_mapping, first_non_empty


def fastmoss_security_browser_fallback_attempted(*sources: Mapping[str, Any]) -> bool:
    for source in sources:
        try:
            attempt_count = int(first_non_empty(source.get("fastmoss_security_browser_fallback_attempt"), 0) or "0")
        except ValueError:
            attempt_count = 0
        if attempt_count > 0:
            return True
        if first_non_empty(source.get("fallback_source_job_id")):
            return True
    return False


def writeback_enabled(*sources: Mapping[str, Any]) -> bool:
    for source in sources:
        if "writeback_enabled" in source:
            return coerce_bool(source.get("writeback_enabled"), default=True)
    return True


def is_unavailable_product_result(payload: Mapping[str, Any]) -> bool:
    product = coerce_mapping(payload.get("product"))
    logical_fields = coerce_mapping(payload.get("logical_fields"))
    candidates = (
        payload.get("availability_status"),
        payload.get("status"),
        product.get("availability_status"),
        product.get("status"),
        logical_fields.get("availability_status"),
        logical_fields.get("status"),
        coerce_mapping(product.get("facts")).get("availability_status"),
        coerce_mapping(product.get("facts")).get("status"),
    )
    normalized = {str(value or "").strip().lower() for value in candidates if str(value or "").strip()}
    return bool(
        normalized
        & {
            "unavailable",
            "off_shelf",
            "off_shelf_or_region_unavailable",
            "region_unavailable",
            "not_available",
            "product_unavailable",
        }
    )
