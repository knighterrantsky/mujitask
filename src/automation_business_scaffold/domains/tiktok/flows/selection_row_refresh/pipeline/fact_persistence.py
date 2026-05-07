from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.shared import first_non_empty


def observation_context(
    *,
    source_record_id: str,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "source_record_id": source_record_id,
        "product_id": first_non_empty(identity.get("product_id")),
        "normalized_product_url": first_non_empty(
            identity.get("normalized_product_url"), identity.get("product_url")
        ),
    }
