from __future__ import annotations

from collections.abc import Mapping
from typing import Any


DEFAULT_MIN_VIDEO_SALES_28D = 50


def normalize_min_video_sales_28d(value: Any) -> int:
    if value in (None, ""):
        return DEFAULT_MIN_VIDEO_SALES_28D
    if isinstance(value, bool):
        raise ValueError("min_video_sales_28d must be a non-negative integer.")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "min_video_sales_28d must be a non-negative integer."
        ) from exc
    if normalized < 0 or str(value).strip() not in {str(normalized), f"{normalized}.0"}:
        raise ValueError("min_video_sales_28d must be a non-negative integer.")
    return normalized


def select_product_video_creator_candidates(
    rows: list[Mapping[str, Any]],
    *,
    product_id: str,
    min_video_sales_28d: Any = DEFAULT_MIN_VIDEO_SALES_28D,
) -> dict[str, Any]:
    threshold = normalize_min_video_sales_28d(min_video_sales_28d)
    seen_video_ids: set[str] = set()
    candidates: dict[str, dict[str, Any]] = {}
    valid_sales_rows: list[dict[str, Any]] = []
    duplicate_video_count = 0
    invalid_identity_count = 0
    invalid_sales_count = 0
    qualified_video_count = 0

    for raw_row in rows:
        row = dict(raw_row)
        video_id = _first_text(
            row.get("video_id"),
            row.get("id"),
            _mapping(row.get("video")).get("video_id"),
            _mapping(row.get("video")).get("id"),
        )
        if not video_id:
            invalid_identity_count += 1
            continue
        if video_id in seen_video_ids:
            duplicate_video_count += 1
            continue
        seen_video_ids.add(video_id)

        creator_identity = _creator_identity(row)
        creator_id = creator_identity["creator_id"]
        if not creator_id:
            invalid_identity_count += 1
            continue
        sales = _numeric(row.get("sold_count"))
        if sales is None:
            invalid_sales_count += 1
            continue
        valid_sales_rows.append(
            {
                "video_id": video_id,
                "creator_id": creator_id,
                "video_product_sales_28d": sales,
            }
        )
        if sales <= threshold:
            continue

        qualified_video_count += 1
        existing = candidates.get(creator_id)
        if existing is None:
            candidates[creator_id] = {
                **creator_identity,
                "product_id": str(product_id).strip(),
                "video_product_sales_28d": sales,
                "winning_video_id": video_id,
                "qualified_video_count": 1,
            }
            continue
        existing["qualified_video_count"] += 1
        if sales > existing["video_product_sales_28d"]:
            existing["video_product_sales_28d"] = sales
            existing["winning_video_id"] = video_id

    return {
        "min_video_sales_28d": threshold,
        "fetched_video_count": len(rows),
        "deduped_video_count": len(seen_video_ids),
        "qualified_video_count": qualified_video_count,
        "duplicate_video_count": duplicate_video_count,
        "invalid_identity_count": invalid_identity_count,
        "invalid_sales_count": invalid_sales_count,
        "candidates": sorted(candidates.values(), key=lambda item: item["creator_id"]),
        "valid_sales_rows": valid_sales_rows,
    }


def aggregate_creator_candidates(
    candidates: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        creator_id = _normalize_unique_id(candidate.get("creator_id"))
        unique_id = _normalize_unique_id(candidate.get("unique_id"))
        uid = _first_text(candidate.get("uid"))
        product_id = _first_text(candidate.get("product_id"))
        sales = _numeric(
            candidate.get("video_product_sales_28d")
            if candidate.get("video_product_sales_28d") not in (None, "")
            else candidate.get("creator_run_max_sales_28d")
        )
        if (
            not creator_id
            or not unique_id
            or creator_id != unique_id
            or not uid
            or not product_id
            or sales is None
        ):
            continue
        group = grouped.setdefault(
            creator_id,
            {
                "creator_id": creator_id,
                "uid": uid,
                "unique_id": unique_id,
                "creator_run_max_sales_28d": sales,
                "product_hits": [],
                "source_product_images": [],
                "holidays": [],
            },
        )
        if sales > group["creator_run_max_sales_28d"]:
            group["creator_run_max_sales_28d"] = sales
        hit = {
            key: value
            for key, value in {
                "product_id": product_id,
                "video_product_sales_28d": sales,
                "winning_video_id": _first_text(candidate.get("winning_video_id")),
                "qualified_video_count": candidate.get("qualified_video_count"),
                "source_record_ids": list(candidate.get("source_record_ids") or []),
                "source_product_images": list(
                    candidate.get("source_product_images") or []
                ),
                "holidays": list(candidate.get("holidays") or []),
            }.items()
            if value not in ("", None, [])
        }
        existing_index = next(
            (
                index
                for index, existing in enumerate(group["product_hits"])
                if existing.get("product_id") == product_id
            ),
            -1,
        )
        if existing_index < 0:
            group["product_hits"].append(hit)
        elif sales > group["product_hits"][existing_index].get(
            "video_product_sales_28d", -1
        ):
            group["product_hits"][existing_index] = hit
        _extend_unique_mappings(
            group["source_product_images"],
            list(candidate.get("source_product_images") or []),
        )
        _extend_unique_text(group["holidays"], list(candidate.get("holidays") or []))
    for group in grouped.values():
        group["product_hits"].sort(key=lambda item: item["product_id"])
    return sorted(grouped.values(), key=lambda item: item["creator_id"])


def _creator_identity(row: Mapping[str, Any]) -> dict[str, str]:
    author = _mapping(row.get("author"))
    root_uid = _first_text(row.get("uid"), row.get("author_uid"))
    author_uid = _first_text(author.get("uid"), author.get("author_uid"))
    if root_uid and author_uid and root_uid != author_uid:
        return {"creator_id": "", "uid": "", "unique_id": ""}
    uid = _first_text(root_uid, author_uid)
    root_unique_id = _normalize_unique_id(
        row.get("unique_id"), row.get("author_unique_id")
    )
    author_unique_id = _normalize_unique_id(author.get("unique_id"))
    if root_unique_id and author_unique_id and root_unique_id != author_unique_id:
        return {"creator_id": "", "uid": "", "unique_id": ""}
    unique_id = _normalize_unique_id(root_unique_id, author_unique_id)
    if not uid or not unique_id:
        return {"creator_id": "", "uid": "", "unique_id": ""}
    return {
        "creator_id": unique_id,
        "uid": uid,
        "unique_id": unique_id,
    }


def _numeric(value: Any) -> int | float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)
    normalized = str(value).strip().replace(",", "")
    if not normalized:
        return None
    try:
        number = float(normalized)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        normalized = str(value).strip() if value is not None else ""
        if normalized:
            return normalized
    return ""


def _normalize_unique_id(*values: Any) -> str:
    return _first_text(*values).lstrip("@").strip()


def _extend_unique_text(target: list[str], values: list[Any]) -> None:
    for value in values:
        normalized = _first_text(value)
        if normalized and normalized not in target:
            target.append(normalized)


def _extend_unique_mappings(
    target: list[dict[str, Any]], values: list[Any]
) -> None:
    seen = {_mapping_identity(item) for item in target}
    for value in values:
        if not isinstance(value, Mapping):
            continue
        identity = _mapping_identity(value)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        target.append(dict(value))


def _mapping_identity(item: Mapping[str, Any]) -> str:
    durable_ref = ""
    if item.get("bucket") and item.get("object_key"):
        durable_ref = (
            f"{_first_text(item.get('bucket'))}/"
            f"{_first_text(item.get('object_key'))}"
        )
    return _first_text(
        item.get("file_token"),
        durable_ref,
        item.get("content_digest"),
        item.get("url"),
        item.get("name"),
    )


__all__ = [
    "DEFAULT_MIN_VIDEO_SALES_28D",
    "aggregate_creator_candidates",
    "normalize_min_video_sales_28d",
    "select_product_video_creator_candidates",
]
