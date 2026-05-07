from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def row_summary(*, source_record_id: str, business_key: str, row_status: str, runtime_evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_record_id": source_record_id,
        "product_business_key": business_key,
        "row_status": row_status,
        "browser_fallback_used": bool(runtime_evidence.get("browser_fallback_used")),
    }
