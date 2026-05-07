from __future__ import annotations


def direct_ingest_enabled(payload: dict[str, object]) -> bool:
    return bool(payload.get("product_url") or payload.get("product_id"))
