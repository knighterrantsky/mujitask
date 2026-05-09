from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.shared import first_non_empty, product_business_key


def row_business_key(*, identity: Mapping[str, Any], source_record_id: str, payload: Mapping[str, Any]) -> str:
    return first_non_empty(payload.get("business_key"), product_business_key(identity), source_record_id)


def runtime_evidence(*, source_record_id: str, business_key: str) -> dict[str, Any]:
    return {
        "source_record_id": source_record_id,
        "product_business_key": business_key,
        "browser_fallback_used": False,
    }
