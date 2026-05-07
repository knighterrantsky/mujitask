from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping


def search_digest(*, search_query: str, filters: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {
            "search_query": str(search_query or "").strip(),
            "filters": dict(filters or {}),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def resolve_product_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    nested = row.get("product_identity")
    base = dict(nested) if isinstance(nested, Mapping) else {}
    product_url = str(
        base.get("product_url")
        or row.get("product_url")
        or row.get("url")
        or row.get("normalized_product_url")
        or ""
    ).strip()
    normalized_product_url = normalize_product_url(product_url)
    product_id = str(
        base.get("product_id")
        or row.get("product_id")
        or row.get("id")
        or row.get("productId")
        or extract_tiktok_product_id(normalized_product_url)
        or ""
    ).strip()
    if not normalized_product_url and product_id:
        normalized_product_url = tiktok_product_url(product_id)
    if not product_url and normalized_product_url:
        product_url = normalized_product_url
    product_key = str(base.get("product_key") or row.get("product_key") or row.get("fastmoss_product_key") or "").strip()
    return {
        "product_id": product_id,
        "product_key": product_key or product_id or normalized_product_url,
        "product_url": product_url,
        "normalized_product_url": normalized_product_url,
    }


def normalize_product_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = re.sub(r"[?#].*$", "", text)
    product_id = extract_tiktok_product_id(normalized)
    if product_id:
        return tiktok_product_url(product_id)
    return normalized


def extract_tiktok_product_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"/(?:pdp|product|detail)/(\d+)", text)
    if match:
        return str(match.group(1))
    return ""


def product_business_entity_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("product:"):
        return text
    return f"product:{text}"


def tiktok_product_url(product_id: str) -> str:
    return f"https://www.tiktok.com/shop/pdp/{product_id}" if product_id else ""
