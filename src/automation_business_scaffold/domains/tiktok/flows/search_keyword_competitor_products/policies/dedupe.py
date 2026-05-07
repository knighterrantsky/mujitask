from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def search_digest(*, search_query: str, filters: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"search_query": str(search_query or "").strip(), "filters": dict(filters or {})},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def product_business_entity_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("product:"):
        return text
    return f"product:{text}"
