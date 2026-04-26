from __future__ import annotations






from automation_business_scaffold.contracts.handler.shared import (
    coerce_mapping,
    coerce_mapping_list,
    coerce_str,
    compact_dict,
    first_non_empty,
)






from collections.abc import (
    Mapping,
)


from datetime import (
    datetime,
    timezone,
)


from typing import Any


def _contract_entities_from_fact_bundle(fact_bundle: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        "creators": [
            _contract_creator_entity(creator)
            for creator in coerce_mapping_list(fact_bundle.get("creators"))
        ],
        "products": [
            _contract_product_entity(product)
            for product in coerce_mapping_list(fact_bundle.get("products"))
        ],
        "shops": [
            _contract_shop_entity(shop)
            for shop in coerce_mapping_list(fact_bundle.get("shops"))
        ],
        "videos": [
            _contract_video_entity(video)
            for video in coerce_mapping_list(fact_bundle.get("videos"))
        ],
    }


def _contract_creator_entity(creator: dict[str, Any]) -> dict[str, Any]:
    facts = coerce_mapping(creator.get("facts"))
    base_info = coerce_mapping(facts.get("base_info"))
    author_index = coerce_mapping(facts.get("author_index"))
    stat_info = coerce_mapping(facts.get("stat_info"))
    cargo_summary = coerce_mapping(facts.get("cargo_summary"))
    author_contact = coerce_mapping(facts.get("author_contact"))
    metrics = _metric_fields(base_info, author_index, stat_info, cargo_summary)
    return compact_dict(
        {
            "entity_key": _contract_entity_key("creator", _creator_entity_ref(creator)),
            "creator_id": first_non_empty(creator.get("creator_id"), creator.get("unique_id")),
            "uid": creator.get("uid"),
            "unique_id": creator.get("unique_id"),
            "nickname": creator.get("nickname"),
            "avatar_url": first_non_empty(base_info.get("avatar"), base_info.get("avatar_url")),
            "region": first_non_empty(creator.get("country_region"), base_info.get("region")),
            "profile_url": creator.get("profile_url"),
            "metrics": metrics,
            "contact": _contract_contact(author_contact),
            "source_platform": creator.get("source_platform"),
        }
    )


def _contract_product_entity(product: dict[str, Any]) -> dict[str, Any]:
    product_id = first_non_empty(product.get("product_id"))
    return compact_dict(
        {
            "entity_key": _contract_entity_key("product", product_id),
            "product_id": product_id,
            "title": product.get("title"),
            "image_url": first_non_empty(product.get("image_url"), product.get("cover_url")),
            "source_platform": product.get("source_platform"),
        }
    )


def _contract_shop_entity(shop: dict[str, Any]) -> dict[str, Any]:
    shop_ref = _shop_entity_ref(shop)
    return compact_dict(
        {
            "entity_key": _contract_entity_key("shop", shop_ref),
            "seller_id": first_non_empty(shop.get("seller_id"), shop.get("shop_id")),
            "shop_id": first_non_empty(shop.get("shop_id"), shop.get("seller_id")),
            "shop_name": shop.get("shop_name"),
            "source_platform": shop.get("source_platform"),
        }
    )


def _contract_video_entity(video: dict[str, Any]) -> dict[str, Any]:
    video_id = first_non_empty(video.get("video_id"))
    return compact_dict(
        {
            "entity_key": _contract_entity_key("video", video_id),
            "video_id": video_id,
            "title": video.get("title"),
            "cover_url": video.get("cover_url"),
            "video_url": video.get("video_url"),
            "source_platform": video.get("source_platform"),
        }
    )


def _contract_relations_from_fact_bundle(fact_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    relations = coerce_mapping(fact_bundle.get("relations"))
    result: list[dict[str, Any]] = []
    for relation in coerce_mapping_list(relations.get("creator_products")):
        result.append(_contract_creator_product_relation(relation))
    for relation in coerce_mapping_list(relations.get("creator_videos")):
        result.append(_contract_creator_video_relation(relation))
    for relation in coerce_mapping_list(relations.get("video_products")):
        result.append(_contract_video_product_relation(relation))
    for relation in coerce_mapping_list(relations.get("shop_creators")):
        result.append(_contract_shop_creator_relation(relation))
    for relation in coerce_mapping_list(relations.get("product_shops")):
        result.append(_contract_product_shop_relation(relation))
    return [relation for relation in result if relation]


def _contract_creator_product_relation(relation: dict[str, Any]) -> dict[str, Any]:
    raw = _relation_raw(relation)
    creator_ref = _creator_ref_from_relation(relation)
    product_id = first_non_empty(relation.get("product_id"))
    return compact_dict(
        {
            "relation_key": f"creator_product:{creator_ref}:{product_id}",
            "relation_type": "creator_promotes_product",
            "from_entity_key": _contract_entity_key("creator", creator_ref),
            "to_entity_key": _contract_entity_key("product", product_id),
            "source": first_non_empty(relation.get("source_platform"), "fastmoss"),
            "metrics": _relation_metric_fields(raw, relation),
            "source_context": _relation_source_context(relation, raw),
        }
    )


def _contract_creator_video_relation(relation: dict[str, Any]) -> dict[str, Any]:
    creator_ref = _creator_ref_from_relation(relation)
    video_id = first_non_empty(relation.get("video_id"), _strip_key_prefix(relation.get("video_key")))
    return compact_dict(
        {
            "relation_key": f"creator_video:{creator_ref}:{video_id}",
            "relation_type": "creator_published_video",
            "from_entity_key": _contract_entity_key("creator", creator_ref),
            "to_entity_key": _contract_entity_key("video", video_id),
            "source": first_non_empty(relation.get("source_platform"), "fastmoss"),
            "source_context": _relation_source_context(relation, _relation_raw(relation)),
        }
    )


def _contract_video_product_relation(relation: dict[str, Any]) -> dict[str, Any]:
    video_id = first_non_empty(relation.get("video_id"), _strip_key_prefix(relation.get("video_key")))
    product_id = first_non_empty(relation.get("product_id"))
    return compact_dict(
        {
            "relation_key": f"video_product:{video_id}:{product_id}",
            "relation_type": "video_mounts_product",
            "from_entity_key": _contract_entity_key("video", video_id),
            "to_entity_key": _contract_entity_key("product", product_id),
            "source": first_non_empty(relation.get("source_platform"), "fastmoss"),
            "source_context": _relation_source_context(relation, _relation_raw(relation)),
        }
    )


def _contract_shop_creator_relation(relation: dict[str, Any]) -> dict[str, Any]:
    shop_ref = _shop_ref_from_relation(relation)
    creator_ref = _creator_ref_from_relation(relation)
    return compact_dict(
        {
            "relation_key": f"shop_creator:{shop_ref}:{creator_ref}",
            "relation_type": "shop_collaborates_with_creator",
            "from_entity_key": _contract_entity_key("shop", shop_ref),
            "to_entity_key": _contract_entity_key("creator", creator_ref),
            "source": first_non_empty(relation.get("source_platform"), "fastmoss"),
            "source_context": _relation_source_context(relation, _relation_raw(relation)),
        }
    )


def _contract_product_shop_relation(relation: dict[str, Any]) -> dict[str, Any]:
    product_id = first_non_empty(relation.get("product_id"))
    shop_ref = _shop_ref_from_relation(relation)
    return compact_dict(
        {
            "relation_key": f"product_shop:{product_id}:{shop_ref}",
            "relation_type": "product_sold_by_shop",
            "from_entity_key": _contract_entity_key("product", product_id),
            "to_entity_key": _contract_entity_key("shop", shop_ref),
            "source": first_non_empty(relation.get("source_platform"), "fastmoss"),
            "source_context": _relation_source_context(relation, _relation_raw(relation)),
        }
    )


def _contract_media_refs_from_fact_bundle(fact_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    media_refs: list[dict[str, Any]] = []
    for asset in coerce_mapping_list(fact_bundle.get("media_assets")):
        entity_type = first_non_empty(asset.get("entity_type"))
        entity_ref = _strip_key_prefix(asset.get("entity_external_id"))
        media_refs.append(
            compact_dict(
                {
                    "entity_key": _contract_entity_key(entity_type, entity_ref),
                    "media_type": first_non_empty(asset.get("media_role"), "media"),
                    "source_url": asset.get("source_url"),
                    "source_platform": asset.get("source_platform"),
                }
            )
        )
    return media_refs


def _contract_contact(payload: dict[str, Any]) -> dict[str, Any]:
    contact_text = first_non_empty(
        _contact_text_from_list(payload.get("list")),
        payload.get("email"),
        payload.get("mail"),
        payload.get("whatsapp"),
        payload.get("phone"),
        payload.get("contact"),
        payload.get("contact_info"),
        payload.get("raw"),
    )
    return compact_dict(
        {
            "raw": contact_text,
            "normalized_text": contact_text,
            "available": bool(contact_text),
        }
    )


def _contact_text_from_list(value: Any) -> str:
    rows = coerce_mapping_list(value)
    if not rows:
        return ""
    email = _first_available_contact(rows, preferred_names={"email", "mail"})
    if email:
        return email
    return _first_available_contact(rows, preferred_names=set())


def _first_available_contact(rows: list[dict[str, Any]], *, preferred_names: set[str]) -> str:
    for row in rows:
        name = coerce_str(row.get("name")).lower()
        if preferred_names and name not in preferred_names:
            continue
        if not bool(row.get("has")):
            continue
        text = (
            first_non_empty(row.get("id"), row.get("link"), row.get("channel_name"), row.get("contact"))
            if name in {"email", "mail"}
            else first_non_empty(row.get("link"), row.get("channel_name"), row.get("id"), row.get("contact"))
        )
        if text:
            return text
    return ""


def _metric_fields(*payloads: Mapping[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for payload in payloads:
        for metric_name, metric_value in _iter_metric_values(payload):
            metrics[metric_name] = metric_value
    return metrics


def _relation_metric_fields(*payloads: Mapping[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for field_name in ("sold_count", "sale_amount", "commission_rate"):
        for payload in payloads:
            metric_value = _metric_number(payload.get(field_name))
            if metric_value is not None:
                metrics[field_name] = metric_value
                break
    return metrics


def _iter_metric_values(payload: Mapping[str, Any], *, prefix: str = "") -> list[tuple[str, Any]]:
    metrics: list[tuple[str, Any]] = []
    ignored_keys = {
        "id",
        "uid",
        "unique_id",
        "creator_id",
        "product_id",
        "video_id",
        "shop_id",
        "seller_id",
        "nickname",
        "name",
        "avatar",
        "avatar_url",
        "region",
        "country_region",
        "update_at",
    }
    for key, value in payload.items():
        key_text = coerce_str(key)
        if not key_text or key_text in ignored_keys:
            continue
        metric_name = f"{prefix}.{key_text}" if prefix else key_text
        if isinstance(value, Mapping):
            metrics.extend(_iter_metric_values(value, prefix=metric_name))
            continue
        metric_value = _metric_number(value)
        if metric_value is not None:
            metrics.append((metric_name, metric_value))
    return metrics


def _metric_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    text = coerce_str(value)
    if not text:
        return None
    normalized = text.replace(",", "").replace("$", "").replace("%", "")
    try:
        parsed = float(normalized)
    except ValueError:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def _relation_raw(relation: dict[str, Any]) -> dict[str, Any]:
    return coerce_mapping(coerce_mapping(relation.get("metadata")).get("raw"))


def _relation_source_context(relation: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    return compact_dict(
        {
            "source_record_id": first_non_empty(relation.get("source_record_id"), raw.get("source_record_id")),
            "target_record_id": relation.get("target_record_id"),
            "holiday": first_non_empty(relation.get("holiday_name"), raw.get("holiday")),
            "table_url": coerce_mapping(relation.get("metadata")).get("table_url"),
        }
    )


def _creator_entity_ref(creator: Mapping[str, Any]) -> str:
    return _strip_key_prefix(
        first_non_empty(
            creator.get("creator_key"),
            creator.get("creator_id"),
            creator.get("uid"),
            creator.get("unique_id"),
        )
    )


def _shop_entity_ref(shop: Mapping[str, Any]) -> str:
    return _strip_key_prefix(
        first_non_empty(
            shop.get("shop_key"),
            shop.get("shop_id"),
            shop.get("seller_id"),
            shop.get("shop_name"),
        )
    )


def _creator_ref_from_relation(relation: Mapping[str, Any]) -> str:
    return _strip_key_prefix(
        first_non_empty(
            relation.get("creator_key"),
            relation.get("creator_id"),
            relation.get("uid"),
            relation.get("unique_id"),
        )
    )


def _shop_ref_from_relation(relation: Mapping[str, Any]) -> str:
    return _strip_key_prefix(
        first_non_empty(
            relation.get("shop_key"),
            relation.get("shop_id"),
            relation.get("seller_id"),
            relation.get("shop_name"),
        )
    )


def _contract_entity_key(entity_type: str, ref: Any) -> str:
    normalized_type = coerce_str(entity_type)
    normalized_ref = _strip_key_prefix(ref)
    if not normalized_type or not normalized_ref:
        return ""
    return f"fastmoss_{normalized_type}:{normalized_ref}"


def _strip_key_prefix(value: Any) -> str:
    text = first_non_empty(value)
    if ":" in text:
        return text.split(":", 1)[1]
    return text


def _coerce_positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
