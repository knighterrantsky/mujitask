from __future__ import annotations
from collections.abc import Mapping
from datetime import date
from typing import Any


def _normalize_write_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    write_mode = _text(payload.get("write_mode"))
    record_id = _first_non_empty(record.get("record_id"), record.get("source_record_id"))
    op = _text(record.get("op"))
    if not op:
        if "insert" in write_mode or "append" in write_mode:
            op = "append"
        elif "upsert" in write_mode:
            op = "upsert"
        elif record_id:
            op = "update"
        else:
            op = "append"
    item = {
        "op": op,
        "record_id": record_id,
        "business_entity_key": _first_non_empty(record.get("business_entity_key"), payload.get("business_entity_key")),
        "upsert_key": _mapping(record.get("upsert_key")),
        "update_excluded_fields": list(record.get("update_excluded_fields") or payload.get("update_excluded_fields") or []),
        "update_replace_fields": list(record.get("update_replace_fields") or payload.get("update_replace_fields") or []),
        "update_accumulate_fields": _mapping(record.get("update_accumulate_fields") or payload.get("update_accumulate_fields")),
        "fields": _mapping(record.get("fields")),
        "source_context": _mapping(record.get("source_context")) or _source_context_from_record(record, payload),
    }
    return _compact(item)


def _map_influencer_pool_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    creator_id = _first_non_empty(record.get("creator_id"), _mapping(record.get("creator_fact_bundle")).get("creator_id"))
    creator_name = _first_non_empty(record.get("creator_name"), _mapping(record.get("creator_fact_bundle")).get("display_name"), _mapping(record.get("creator_fact_bundle")).get("nickname"))
    product_id = _text(record.get("product_id"))
    related_product_sales = _stringify_scalar(_first_non_empty(record.get("matched_product_sold_count"), _relation_metric(record, "sold_count")))
    related_product_sales_delta = _stringify_scalar(_first_non_empty(record.get("matched_product_sold_delta"), related_product_sales))
    fields = _compact(
        {
            "达人ID": creator_id,
            "带货商品图": _influencer_product_image_refs(record, product_id=product_id),
            "关联节日": _list_text(record.get("holiday")),
            "关联商品销量": related_product_sales,
            "达人头像": _influencer_avatar_refs(record),
            "粉丝数": _format_w_unit_display(_creator_metric(record, "follower_count", "fans_count")),
            "28天视频数": _stringify_scalar(_creator_metric(record, "aweme_28d_count", "aweme_28_count", "video_count")),
            "带货视频 GMV": _format_w_unit_display(_creator_metric(record, "video_sale_amount", "video_gmv")),
            "带货直播 GMV": _format_w_unit_display(_creator_metric(record, "live_sale_amount", "live_gmv")),
            "合作店铺": _influencer_shop_names(record),
            "达人联系方式": _creator_contact_text(record),
            "记录日期": date.today().isoformat(),
            "更新日期": date.today().isoformat(),
        }
    )
    return _normalize_write_record(
        {
            "op": "upsert",
            "business_entity_key": _first_non_empty(record.get("business_entity_key"), f"creator:{creator_id}" if creator_id else ""),
            "upsert_key": {"field": "达人ID", "value": creator_id} if creator_id else {},
            "fields": fields,
            "update_excluded_fields": ["记录日期"],
            "update_replace_fields": ["达人头像"],
            "update_accumulate_fields": {"关联商品销量": related_product_sales_delta} if related_product_sales_delta else {},
            "source_context": _source_context_from_record(record, payload),
        },
        payload,
    )


def _creator_metric(record: Mapping[str, Any], *names: str) -> Any:
    creator_fact = _mapping(record.get("creator_fact_bundle"))
    metrics = _mapping(creator_fact.get("metrics"))
    for name in names:
        if metrics.get(name) not in (None, ""):
            return metrics.get(name)
    facts = _mapping(creator_fact.get("facts"))
    for section_name in ("base_info", "author_index", "stat_info", "cargo_summary", "raw"):
        section = _mapping(facts.get(section_name))
        for name in names:
            if section.get(name) not in (None, ""):
                return section.get(name)
    for observation in _mapping_list(record.get("observations")):
        metric_name = _text(observation.get("metric_name"))
        if metric_name in names and observation.get("metric_value") not in (None, ""):
            return observation.get("metric_value")
    return ""


def _relation_metric(record: Mapping[str, Any], *names: str) -> Any:
    for relation in _mapping_list(record.get("product_relations")) + _mapping_list(record.get("relations")):
        if _text(relation.get("relation_type")) and _text(relation.get("relation_type")) != "creator_promotes_product":
            continue
        metrics = _mapping(relation.get("metrics"))
        for name in names:
            if metrics.get(name) not in (None, ""):
                return metrics.get(name)
        raw = _mapping(_mapping(relation.get("metadata")).get("raw"))
        for name in names:
            if raw.get(name) not in (None, ""):
                return raw.get(name)
        for name in names:
            if relation.get(name) not in (None, ""):
                return relation.get(name)
    fact_relations = _mapping(_mapping(record.get("fact_bundle")).get("relations"))
    for relation in _mapping_list(fact_relations.get("creator_products")):
        for name in names:
            if relation.get(name) not in (None, ""):
                return relation.get(name)
    return ""


def _creator_contact_text(record: Mapping[str, Any]) -> str:
    creator_fact = _mapping(record.get("creator_fact_bundle"))
    contact = _mapping(creator_fact.get("contact"))
    return _first_non_empty(
        contact.get("normalized_text"),
        contact.get("raw"),
        _mapping(_mapping(creator_fact.get("facts")).get("author_contact")).get("email"),
        _mapping(_mapping(creator_fact.get("facts")).get("author_contact")).get("contact"),
    )


def _influencer_avatar_refs(record: Mapping[str, Any]) -> list[dict[str, str]]:
    creator_fact = _mapping(record.get("creator_fact_bundle"))
    avatar_url = _first_non_empty(creator_fact.get("avatar_url"))
    refs = _media_refs_for(record, entity_type="creator", media_roles={"creator_avatar", "avatar"})
    if avatar_url:
        refs.insert(0, {"url": avatar_url})
    return _dedupe_ref_items(refs)


def _influencer_product_image_refs(record: Mapping[str, Any], *, product_id: str) -> list[dict[str, str]]:
    refs = _attachment_ref_items(record.get("source_product_images"))
    if refs:
        return refs
    refs = _media_refs_for(record, entity_type="product", media_roles={"product_image", "source_product_image"})
    if refs:
        return refs
    fact_bundle = _mapping(record.get("fact_bundle"))
    for asset in _mapping_list(fact_bundle.get("media_assets")):
        if _text(asset.get("entity_type")) != "product":
            continue
        if product_id and _text(asset.get("entity_external_id")) != product_id:
            continue
        refs.extend(_attachment_ref_items([asset]))
    return _dedupe_ref_items(refs)


def _media_refs_for(record: Mapping[str, Any], *, entity_type: str, media_roles: set[str]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for media_ref in _mapping_list(record.get("media_refs")):
        entity_key = _text(media_ref.get("entity_key"))
        role = _text(media_ref.get("media_type") or media_ref.get("media_role"))
        if entity_type and f"_{entity_type}:" not in entity_key and not entity_key.startswith(f"{entity_type}:"):
            continue
        if role and role not in media_roles:
            continue
        refs.extend(_attachment_ref_items([media_ref]))
    return refs


def _attachment_ref_items(value: Any) -> list[dict[str, str]]:
    values = value if isinstance(value, list) else [value]
    refs: list[dict[str, str]] = []
    for item in values:
        if isinstance(item, Mapping):
            file_token = _first_non_empty(item.get("file_token"))
            if file_token:
                refs.append({"file_token": file_token})
                continue
            url = _first_non_empty(
                item.get("url"),
                item.get("source_url"),
                item.get("tmp_url"),
                item.get("download_url"),
                item.get("link"),
            )
            if url:
                refs.append({"url": url})
            continue
        text = _text(item)
        if text:
            refs.append({"url": text})
    return _dedupe_ref_items(refs)


def _dedupe_ref_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (_text(item.get("file_token")), _text(item.get("url")))
        if not any(key) or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _influencer_shop_names(record: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for value in _list_text(record.get("cooperation_shop_names")):
        if value and value not in names:
            names.append(value)

    shop_refs = _cooperation_shop_refs(record)
    shops_by_ref = _shops_by_ref(record)
    for shop_ref in shop_refs:
        shop = shops_by_ref.get(shop_ref)
        name = _first_non_empty(_mapping(shop).get("shop_name"), _mapping(shop).get("name"))
        if name and name not in names:
            names.append(name)

    if names:
        return names

    fact_bundle = _mapping(record.get("fact_bundle"))
    for relation in _mapping_list(_mapping(fact_bundle.get("relations")).get("shop_creators")):
        name = _first_non_empty(relation.get("shop_name"), _mapping(_mapping(relation.get("metadata")).get("raw")).get("shop_name"))
        if name and name not in names:
            names.append(name)
    return names


def _cooperation_shop_refs(record: Mapping[str, Any]) -> list[str]:
    refs: list[str] = []
    for relation in _mapping_list(record.get("relations")):
        if _text(relation.get("relation_type")) != "shop_collaborates_with_creator":
            continue
        ref = _strip_entity_ref(_first_non_empty(relation.get("from_entity_key"), relation.get("shop_key"), relation.get("shop_id"), relation.get("seller_id")))
        if ref and ref not in refs:
            refs.append(ref)
    fact_bundle = _mapping(record.get("fact_bundle"))
    for relation in _mapping_list(_mapping(fact_bundle.get("relations")).get("shop_creators")):
        ref = _strip_entity_ref(_first_non_empty(relation.get("shop_key"), relation.get("shop_id"), relation.get("seller_id"), relation.get("shop_name")))
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _shops_by_ref(record: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    shops: dict[str, Mapping[str, Any]] = {}
    for shop in _mapping_list(_mapping(record.get("entities")).get("shops")):
        for ref in _shop_refs(shop):
            shops.setdefault(ref, shop)
    for shop in _mapping_list(_mapping(record.get("fact_bundle")).get("shops")):
        for ref in _shop_refs(shop):
            shops.setdefault(ref, shop)
    return shops


def _shop_refs(shop: Mapping[str, Any]) -> list[str]:
    refs: list[str] = []
    for value in (shop.get("entity_key"), shop.get("shop_key"), shop.get("shop_id"), shop.get("seller_id"), shop.get("shop_name"), shop.get("name")):
        ref = _strip_entity_ref(value)
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _strip_entity_ref(value: Any) -> str:
    text = _text(value)
    if ":" in text:
        return text.split(":", 1)[1]
    return text


def _format_w_unit_display(value: Any) -> str:
    if value in (None, ""):
        return ""
    number = _number_value(value)
    if number is None:
        return _text(value)
    if abs(number) >= 10_000:
        sign = "-" if number < 0 else ""
        return f"{sign}{int(abs(number) / 10_000 + 0.5)}W"
    return "小于1W"


def _stringify_scalar(value: Any) -> str:
    if value in (None, ""):
        return ""
    number = _number_value(value)
    if number is not None:
        return _format_trimmed_decimal(number)
    return _text(value)


def _number_value(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _text(value).replace(",", "").replace("$", "").replace(" ", "")
    multiplier = 1.0
    lower = text.lower()
    for suffix, value_multiplier in (("亿", 100_000_000.0), ("万", 10_000.0), ("w", 10_000.0), ("m", 1_000_000.0), ("k", 1_000.0)):
        if lower.endswith(suffix):
            multiplier = value_multiplier
            text = text[: -len(suffix)]
            break
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def _format_trimmed_decimal(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def influencer_pool_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _map_influencer_pool_record(record, payload)


def _source_context_from_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    return _compact(
        {
            "source_record_id": _text(record.get("source_record_id") or payload.get("source_record_id")),
            "candidate_key": _text(record.get("candidate_key") or payload.get("candidate_key")),
            "workflow_code": _text(payload.get("workflow_code")),
            "stage_code": _text(payload.get("stage_code")),
            "projection_type": _text(payload.get("mapper_code")),
        }
    )


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _list_text(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_text(item) for item in value if _text(item) or item == ""]
    if isinstance(value, tuple):
        return [_text(item) for item in value if _text(item) or item == ""]
    text = _text(value)
    return [text] if text else []


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _compact(payload: Mapping[str, Any]) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                compacted[str(key)] = value.strip()
            continue
        if isinstance(value, Mapping):
            nested = _compact(value)
            if nested:
                compacted[str(key)] = nested
            continue
        if isinstance(value, list):
            items = [item for item in value if item not in ("", None, {}, [])]
            if items:
                compacted[str(key)] = items
            continue
        compacted[str(key)] = value
    return compacted
