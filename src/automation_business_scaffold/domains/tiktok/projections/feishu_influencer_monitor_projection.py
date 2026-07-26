from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any


INFLUENCER_MONITOR_FIELD_ALLOWLIST = (
    "达人ID",
    "带货商品图",
    "关联节日",
    "关联商品销量",
    "达人头像",
    "粉丝数",
    "28天视频数",
    "带货视频 GMV",
    "带货直播 GMV",
    "合作店铺",
    "达人联系方式",
    "记录日期",
    "更新日期",
)


def influencer_monitor_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    creator_fact = _mapping(record.get("creator_fact_bundle"))
    creator_id = _normalize_unique_id(
        record.get("creator_unique_id"),
        record.get("creator_id"),
        creator_fact.get("unique_id"),
    )
    today = _first_text(record.get("write_date"), payload.get("write_date"), date.today())
    fields = _compact(
        {
            "达人ID": creator_id,
            "带货商品图": _attachment_refs(record.get("source_product_images")),
            "关联节日": _unique_text(record.get("holidays")),
            "关联商品销量": _scalar(
                record.get("creator_run_max_sales_28d")
                if record.get("creator_run_max_sales_28d") not in (None, "")
                else record.get("video_product_sales_28d")
            ),
            "达人头像": _avatar_refs(record),
            "粉丝数": _format_w(
                _creator_metric(creator_fact, "follower_count", "fans_count")
            ),
            "28天视频数": _scalar(
                _creator_metric(
                    creator_fact,
                    "aweme_28d_count",
                    "aweme_28_count",
                    "video_count",
                )
            ),
            "带货视频 GMV": _format_w(
                _creator_metric(creator_fact, "video_sale_amount", "video_gmv")
            ),
            "带货直播 GMV": _format_w(
                _creator_metric(creator_fact, "live_sale_amount", "live_gmv")
            ),
            "合作店铺": _shop_names(record),
            "达人联系方式": _contact_text(creator_fact),
            "记录日期": today,
            "更新日期": today,
        }
    )
    return _compact(
        {
            "op": "upsert",
            "business_entity_key": f"creator:{creator_id}" if creator_id else "",
            "upsert_key": {"field": "达人ID", "value": creator_id}
            if creator_id
            else {},
            "fields": fields,
            "update_excluded_fields": ["记录日期"],
            "update_replace_fields": ["达人头像"],
            "update_merge_strategies": {"关联商品销量": "max_numeric"},
            "skip_unchanged_update_fields": True,
            "conditional_update_fields": ["更新日期"],
            "source_context": {
                "creator_id": creator_id,
                "product_ids": [
                    _first_text(hit.get("product_id"))
                    for hit in _mapping_list(record.get("product_hits"))
                    if _first_text(hit.get("product_id"))
                ],
                "workflow_code": _first_text(payload.get("workflow_code")),
                "stage_code": _first_text(payload.get("stage_code")),
            },
        }
    )


def _creator_metric(creator_fact: Mapping[str, Any], *names: str) -> Any:
    metrics = _mapping(creator_fact.get("metrics"))
    for name in names:
        if metrics.get(name) not in (None, ""):
            return metrics[name]
    facts = _mapping(creator_fact.get("facts"))
    for section_name in (
        "base_info",
        "author_index",
        "stat_info",
        "cargo_summary",
        "raw",
    ):
        section = _mapping(facts.get(section_name))
        for name in names:
            if section.get(name) not in (None, ""):
                return section[name]
    return ""


def _normalize_unique_id(*values: Any) -> str:
    return _first_text(*values).lstrip("@").strip()


def _contact_text(creator_fact: Mapping[str, Any]) -> str:
    contact = _mapping(creator_fact.get("contact"))
    raw = _mapping(_mapping(creator_fact.get("facts")).get("author_contact"))
    return _first_text(
        contact.get("normalized_text"),
        contact.get("raw"),
        raw.get("email"),
        raw.get("contact"),
    )


def _avatar_refs(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in _mapping_list(record.get("media_refs")):
        entity_type = _first_text(item.get("entity_type")).lower()
        entity_key = _first_text(item.get("entity_key")).lower()
        role = _first_text(item.get("media_role"), item.get("media_type")).lower()
        if entity_type != "creator" and not entity_key.startswith("creator:"):
            continue
        if role not in {"creator_avatar", "avatar"}:
            continue
        refs.extend(_attachment_refs([item], durable_only=True))
    return _dedupe_refs(refs)


def _attachment_refs(
    value: Any,
    *,
    durable_only: bool = False,
) -> list[dict[str, Any]]:
    values = value if isinstance(value, list) else [value]
    refs: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        bucket = _first_text(item.get("bucket"))
        object_key = _first_text(item.get("object_key"))
        digest = _first_text(item.get("content_digest"))
        durable = bool(
            bucket and object_key and re.fullmatch(r"[0-9a-f]{64}", digest)
        )
        if durable:
            refs.append(
                {
                    key: item_value
                    for key, item_value in {
                        "bucket": bucket,
                        "object_key": object_key,
                        "content_digest": digest,
                        "file_name": _first_text(
                            item.get("file_name"), item.get("name")
                        ),
                        "mime_type": _first_text(item.get("mime_type")),
                    }.items()
                    if item_value
                }
            )
            continue
        if durable_only:
            continue
        file_token = _first_text(item.get("file_token"))
        if file_token:
            refs.append(
                {
                    key: item_value
                    for key, item_value in {
                        "file_token": file_token,
                        "name": _first_text(item.get("name"), item.get("file_name")),
                    }.items()
                    if item_value
                }
            )
    return _dedupe_refs(refs)


def _dedupe_refs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        durable_ref = ""
        if item.get("bucket") and item.get("object_key"):
            durable_ref = (
                f"{_first_text(item.get('bucket'))}/"
                f"{_first_text(item.get('object_key'))}"
            )
        identity = _first_text(
            item.get("file_token"),
            durable_ref,
            item.get("content_digest"),
        )
        if identity and identity not in seen:
            seen.add(identity)
            result.append(item)
    return result


def _shop_names(record: Mapping[str, Any]) -> list[str]:
    names = _unique_text(record.get("cooperation_shop_names"))
    for shop in _mapping_list(_mapping(record.get("fact_bundle")).get("shops")):
        name = _first_text(shop.get("shop_name"), shop.get("name"))
        if name and name not in names:
            names.append(name)
    return names


def _format_w(value: Any) -> str:
    number = _number(value)
    if number is None:
        return ""
    if abs(number) < 10_000:
        return "小于1W"
    sign = "-" if number < 0 else ""
    return f"{sign}{int(abs(number) / 10_000 + 0.5)}W"


def _scalar(value: Any) -> str:
    number = _number(value)
    if number is None:
        return _first_text(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _unique_text(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple)) else [value]
    result: list[str] = []
    for item in values:
        normalized = _first_text(
            item.get("name") if isinstance(item, Mapping) else item
        )
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _first_text(*values: Any) -> str:
    for value in values:
        normalized = str(value).strip() if value is not None else ""
        if normalized:
            return normalized
    return ""


def _compact(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if item in (None, "", [], {}):
            continue
        if isinstance(item, Mapping):
            nested = _compact(item)
            if nested:
                result[str(key)] = nested
            continue
        result[str(key)] = item
    return result


__all__ = [
    "INFLUENCER_MONITOR_FIELD_ALLOWLIST",
    "influencer_monitor_projection_mapper",
]
