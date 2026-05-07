from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.shared import compact_dict, first_non_empty


def projection_record(
    *,
    source_record_id: str,
    business_key: str,
    product_id: str,
    product_url: str,
    projection_fields: Mapping[str, Any],
    source_fields: Mapping[str, Any] | None = None,
    source_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return compact_dict(
        {
            "source_record_id": source_record_id,
            "business_entity_key": business_key,
            "product_id": first_non_empty(product_id),
            "product_url": first_non_empty(product_url),
            "projection_fields": dict(projection_fields),
            "source_fields": dict(source_fields or {}),
            "source_context": dict(source_context or {}),
        }
    )
