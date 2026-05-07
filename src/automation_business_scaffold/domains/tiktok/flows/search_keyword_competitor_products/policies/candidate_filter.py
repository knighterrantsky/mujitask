from __future__ import annotations

from typing import Any, Mapping


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
