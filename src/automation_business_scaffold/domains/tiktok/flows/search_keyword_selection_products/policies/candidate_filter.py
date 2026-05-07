from __future__ import annotations

from typing import Any, Mapping

from .dedupe import product_business_entity_key, resolve_product_identity


def normalize_search_candidates(
    raw_candidates: Any,
    *,
    search_query: str,
    output_conditions: Mapping[str, Any],
    max_candidates: int,
) -> list[dict[str, Any]]:
    if not isinstance(raw_candidates, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(raw_candidates, start=1):
        if not isinstance(row, Mapping):
            continue
        product_identity = resolve_product_identity(row)
        raw_entity_key = str(
            product_identity.get("product_id")
            or product_identity.get("normalized_product_url")
            or product_identity.get("product_url")
            or product_identity.get("product_key")
            or row.get("candidate_key")
            or index
        )
        business_entity_key = product_business_entity_key(raw_entity_key)
        if not business_entity_key or business_entity_key in seen:
            continue
        candidate_context = {
            "candidate_key": business_entity_key,
            "business_entity_key": business_entity_key,
            "product_identity": product_identity,
            "product_id": str(product_identity.get("product_id") or ""),
            "product_url": str(product_identity.get("product_url") or ""),
            "normalized_product_url": str(product_identity.get("normalized_product_url") or ""),
            "search_query": search_query,
            "search_rank": int(row.get("rank") or index),
            "source_context": dict(row),
        }
        if not candidate_allowed(candidate_context, output_conditions):
            continue
        normalized.append(candidate_context)
        seen.add(business_entity_key)
        if max_candidates > 0 and len(normalized) >= max_candidates:
            break
    return normalized


def candidate_allowed(candidate: Mapping[str, Any], conditions: Mapping[str, Any]) -> bool:
    allowed_ids = {str(item) for item in conditions.get("allowed_product_ids") or [] if str(item)}
    excluded_ids = {str(item) for item in conditions.get("exclude_product_ids") or [] if str(item)}
    require_url = bool(conditions.get("require_product_url", False))
    product_id = str(candidate.get("product_id") or "")
    normalized_product_url = str(candidate.get("normalized_product_url") or "")
    if allowed_ids and product_id not in allowed_ids:
        return False
    if excluded_ids and product_id in excluded_ids:
        return False
    if require_url and not normalized_product_url:
        return False
    return True
