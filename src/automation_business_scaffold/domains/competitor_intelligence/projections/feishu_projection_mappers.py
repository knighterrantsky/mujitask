from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class FeishuProjectionMapperError(Exception):
    error_type: str
    error_code: str
    message: str
    retryable: bool
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)


_PRODUCT_ID_PATTERNS = (
    re.compile(r"/(?:pdp|product|detail)/(\d+)", re.IGNORECASE),
    re.compile(r"[?&](?:product_id|goods_id)=(\d+)", re.IGNORECASE),
    re.compile(r"\b(\d{8,})\b"),
)
_COMPETITOR_SYSTEM_OVERWRITE_FIELDS = {"商品状态"}

ProjectionMapper = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]


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
        "fields": _mapping(record.get("fields")),
        "source_context": _mapping(record.get("source_context")) or _source_context_from_record(record, payload),
    }
    return _compact(item)

def _map_competitor_seed_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    product_id = _first_non_empty(record.get("product_id"), _extract_product_id(record.get("product_url")))
    product_url = _normalize_product_url(
        _first_non_empty(record.get("product_url"), f"https://www.tiktok.com/shop/pdp/{product_id}" if product_id else "")
    )
    search_query = _text(record.get("search_query"))
    fields = {
        "SKU-ID": product_id,
        "产品链接": _link_value(product_url),
        "关键词": search_query,
        "备注": f"通过搜索关键字：{search_query}" if search_query else "",
        "达人查找状态": "待查找",
    }
    upsert_key = (
        {"field": "SKU-ID", "value": product_id}
        if product_id
        else {"field": "产品链接", "value": product_url}
    )
    return _normalize_write_record(
        {
            "op": "insert_if_absent",
            "business_entity_key": _candidate_key(
                {
                    "product_id": product_id,
                    "business_entity_key": record.get("business_entity_key"),
                    "product_url": product_url,
                }
            ),
            "upsert_key": upsert_key,
            "fields": fields,
            "source_context": _source_context_from_record(record, payload),
        },
        payload,
    )


def _map_competitor_table_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    product_id = _text(record.get("product_id"))
    product_url = _normalize_product_url(record.get("product_url"))
    projection_fields = _mapping(record.get("projection_fields"))
    if projection_fields:
        projection_fields = _normalize_competitor_projection_fields(
            {
                "SKU-ID": product_id,
                "产品链接": product_url,
                **projection_fields,
            }
        )
        fields = _select_missing_competitor_projection_fields(
            projection_fields,
            existing_fields=_mapping(record.get("source_fields")),
        )
    else:
        fields = {
            "SKU-ID": product_id,
            "产品链接": _link_value(product_url),
            "记录日期": date.today().isoformat(),
            "备注": _refresh_note(record),
        }
    return _normalize_write_record(
        {
            "op": "update" if _text(record.get("source_record_id")) else "upsert",
            "record_id": _text(record.get("source_record_id")),
            "business_entity_key": _first_non_empty(record.get("business_entity_key"), _candidate_key({"product_id": product_id, "product_url": product_url})),
            "upsert_key": {"field": "SKU-ID", "value": product_id} if product_id else {},
            "fields": fields,
            "source_context": _source_context_from_record(record, payload),
        },
        payload,
    )


def _normalize_competitor_projection_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for field_name, value in fields.items():
        name = _text(field_name)
        if not name or value in (None, "", [], {}):
            continue
        if name in {"产品链接", "图片", "前台截图", "Fastmoss截图"}:
            normalized[name] = _link_value(_text_value(value)) if _text_value(value) else value
            continue
        normalized[name] = value
    return normalized


def _select_missing_competitor_projection_fields(
    projection_fields: Mapping[str, Any],
    *,
    existing_fields: Mapping[str, Any],
) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    for field_name, value in projection_fields.items():
        if field_name == "记录日期":
            continue
        if not _field_has_value(value):
            continue
        if field_name in _COMPETITOR_SYSTEM_OVERWRITE_FIELDS:
            selected[field_name] = value
            continue
        if not _field_has_value(existing_fields.get(field_name)):
            selected[field_name] = value
    if selected:
        selected["记录日期"] = projection_fields.get("记录日期") or date.today().isoformat()
    return selected


def _map_influencer_pool_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    creator_id = _first_non_empty(record.get("creator_id"), _mapping(record.get("creator_fact_bundle")).get("creator_id"))
    creator_name = _first_non_empty(record.get("creator_name"), _mapping(record.get("creator_fact_bundle")).get("display_name"), _mapping(record.get("creator_fact_bundle")).get("nickname"))
    product_id = _text(record.get("product_id"))
    fields = _compact(
        {
            "达人ID": creator_id,
            "达人昵称": creator_name,
            "关联商品ID": product_id,
            "带货商品图": _influencer_product_image_refs(record, product_id=product_id),
            "关联节日": _list_text(_first_non_empty(record.get("holiday"))),
            "关联商品销量": _stringify_scalar(_first_non_empty(record.get("matched_product_sold_count"), _relation_metric(record, "sold_count"))),
            "达人头像": _influencer_avatar_refs(record),
            "粉丝数": _format_w_unit_display(_creator_metric(record, "follower_count", "fans_count")),
            "28天视频数": _stringify_scalar(_creator_metric(record, "aweme_28d_count", "aweme_28_count", "video_count")),
            "带货视频 GMV": _format_w_unit_display(_creator_metric(record, "video_sale_amount", "video_gmv")),
            "带货直播 GMV": _format_w_unit_display(_creator_metric(record, "live_sale_amount", "live_gmv")),
            "合作店铺": _influencer_shop_names(record),
            "达人联系方式": _creator_contact_text(record),
            "记录时间": date.today().isoformat(),
        }
    )
    return _normalize_write_record(
        {
            "op": "upsert",
            "business_entity_key": _first_non_empty(record.get("business_entity_key"), f"creator:{creator_id}" if creator_id else ""),
            "upsert_key": {"field": "达人ID", "value": creator_id} if creator_id else {},
            "fields": fields,
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
    entities = _mapping(record.get("entities"))
    for shop in _mapping_list(entities.get("shops")):
        name = _first_non_empty(shop.get("shop_name"), shop.get("name"))
        if name and name not in names:
            names.append(name)
    fact_bundle = _mapping(record.get("fact_bundle"))
    for shop in _mapping_list(fact_bundle.get("shops")):
        name = _first_non_empty(shop.get("shop_name"), shop.get("name"))
        if name and name not in names:
            names.append(name)
    return names


def _format_w_unit_display(value: Any) -> str:
    if value in (None, ""):
        return ""
    number = _number_value(value)
    if number is None:
        return _text(value)
    if abs(number) >= 10_000:
        return f"{_format_trimmed_decimal(number / 10_000)}W"
    return _format_trimmed_decimal(number)


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


def _map_competitor_influencer_status_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    status = _text(record.get("influencer_sync_status"))
    status_text = {
        "success": "已完成",
        "partial_success": "部分完成",
        "failed": "失败重试",
        "skipped": "跳过",
    }.get(status, status or "已完成")
    fields = {
        "达人查找状态": status_text,
        "达人数量": _coerce_int(
            _first_non_empty(
                record.get("influencer_write_success_count"),
                record.get("creator_detail_success_count"),
                record.get("creator_candidate_count"),
            ),
            default=0,
            minimum=0,
            maximum=1_000_000,
        ),
        "备注": _status_note(record),
    }
    return _normalize_write_record(
        {
            "op": "update",
            "record_id": _text(record.get("source_record_id")),
            "business_entity_key": _first_non_empty(record.get("business_entity_key"), record.get("product_key"), _candidate_key({"product_id": record.get("product_id")})),
            "fields": fields,
            "source_context": _source_context_from_record(record, payload),
        },
        payload,
    )


def _map_selection_table_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    product_identity = _mapping(payload.get("product_identity")) or _mapping(record.get("product_identity"))
    product_id = _first_non_empty(record.get("product_id"), product_identity.get("product_id"))
    product_url = _normalize_product_url(_first_non_empty(record.get("product_url"), product_identity.get("normalized_product_url"), product_identity.get("product_url")))
    fields = {
        "商品ID": product_id,
        "商品链接": _link_value(product_url),
        "记录日期": date.today().isoformat(),
    }
    return _normalize_write_record(
        {
            "op": "update" if _text(record.get("source_record_id")) else "upsert",
            "record_id": _text(record.get("source_record_id")),
            "business_entity_key": _first_non_empty(record.get("business_entity_key"), _candidate_key({"product_id": product_id, "product_url": product_url})),
            "upsert_key": {"field": "商品ID", "value": product_id} if product_id else {},
            "fields": fields,
            "source_context": _source_context_from_record(record, payload),
        },
        payload,
    )


def _selection_writeback_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    product_identity = _mapping(payload.get("product_identity"))
    request_payload = _mapping(payload.get("request_payload"))
    record_id = _first_non_empty(payload.get("selection_record_id"), request_payload.get("selection_record_id"))
    if not (product_identity or record_id):
        return []
    return [
        {
            "source_record_id": record_id,
            "product_identity": product_identity,
            "product_id": _first_non_empty(product_identity.get("product_id"), payload.get("product_id")),
            "product_url": _first_non_empty(product_identity.get("normalized_product_url"), product_identity.get("product_url"), payload.get("product_url")),
        }
    ]


def competitor_seed_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _map_competitor_seed_record(record, payload)


def competitor_table_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _map_competitor_table_record(record, payload)


def influencer_pool_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _map_influencer_pool_record(record, payload)


def competitor_influencer_status_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _map_competitor_influencer_status_record(record, payload)


def selection_table_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _map_selection_table_record(record, payload)


PROJECTION_MAPPERS = MappingProxyType(
    {
        "competitor_seed_projection_mapper": competitor_seed_projection_mapper,
        "competitor_table_projection_mapper": competitor_table_projection_mapper,
        "influencer_pool_projection_mapper": influencer_pool_projection_mapper,
        "competitor_influencer_status_projection_mapper": competitor_influencer_status_projection_mapper,
        "selection_table_projection_mapper": selection_table_projection_mapper,
    }
)
PROJECTION_MAPPER_CODES = frozenset(PROJECTION_MAPPERS)


def get_projection_mapper(mapper_code: str) -> ProjectionMapper | None:
    return PROJECTION_MAPPERS.get(mapper_code)


def map_projection_record(
    mapper_code: str,
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_mapper_code = mapper_code or "selection_table_projection_mapper"
    mapper = get_projection_mapper(normalized_mapper_code)
    if mapper is None:
        raise FeishuProjectionMapperError(
            error_type="configuration_error",
            error_code="unsupported_mapper",
            message=f"Unsupported Feishu projection mapper: {normalized_mapper_code}",
            retryable=False,
            details={"mapper_code": normalized_mapper_code},
        )
    return mapper(record, payload)


def selection_writeback_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _selection_writeback_records(payload)


def _product_identity_from_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    product_url = _field_text(fields, "产品链接", "商品链接", "product_url", "normalized_product_url")
    product_id = _first_non_empty(
        _field_text(fields, "SKU-ID", "SKU ID", "商品ID", "product_id", "sku_id"),
        _extract_product_id(product_url),
    )
    normalized_url = _normalize_product_url(product_url or (f"https://www.tiktok.com/shop/pdp/{product_id}" if product_id else ""))
    return _compact(
        {
            "product_id": product_id,
            "product_url": product_url or normalized_url,
            "normalized_product_url": normalized_url,
            "fastmoss_product_url": f"https://www.fastmoss.com/zh/e-commerce/detail/{product_id}" if product_id else "",
        }
    )


def _identity_matches(identity: Mapping[str, Any], target: Mapping[str, Any]) -> bool:
    identity_product_id = _text(identity.get("product_id"))
    target_product_id = _text(target.get("product_id"))
    if identity_product_id and target_product_id and identity_product_id == target_product_id:
        return True
    identity_url = _normalize_product_url(identity.get("normalized_product_url") or identity.get("product_url"))
    target_url = _normalize_product_url(target.get("normalized_product_url") or target.get("product_url"))
    return bool(identity_url and target_url and identity_url == target_url)


def _candidate_key(identity: Any) -> str:
    item = _mapping(identity)
    value = _first_non_empty(
        item.get("product_id"),
        _strip_product_key_prefix(item.get("business_entity_key")),
        item.get("normalized_product_url"),
        item.get("product_url"),
    )
    return f"product:{value}" if value else ""


def _strip_product_key_prefix(value: Any) -> str:
    text = _text(value)
    return text.removeprefix("product:") if text.startswith("product:") else text


def _extract_product_id(*values: Any) -> str:
    for value in values:
        text = _text_value(value)
        if not text:
            continue
        for pattern in _PRODUCT_ID_PATTERNS:
            match = pattern.search(text)
            if match is not None:
                return match.group(1)
    return ""


def _normalize_product_url(value: Any) -> str:
    text = _text_value(value)
    product_id = _extract_product_id(text)
    if product_id:
        return f"https://www.tiktok.com/shop/pdp/{product_id}"
    return text


def _field_text(fields: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = fields.get(name)
        text = _text_value(value)
        if text:
            return text
    return ""


def _field_has_value(value: Any) -> bool:
    if isinstance(value, list):
        return any(_field_has_value(item) for item in value)
    if isinstance(value, Mapping):
        return any(_field_has_value(item) for item in value.values())
    return bool(_text(value))


def _text_value(value: Any) -> str:
    if isinstance(value, Mapping):
        return _first_non_empty(value.get("link"), value.get("text"), value.get("value"), value.get("name"))
    if isinstance(value, list):
        return _first_non_empty(*(_text_value(item) for item in value))
    return _text(value)


def _link_value(url: str) -> dict[str, str] | str:
    normalized = _normalize_product_url(url)
    if not normalized:
        return ""
    return {"text": normalized, "link": normalized}


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


def _refresh_note(record: Mapping[str, Any]) -> str:
    status = _text(record.get("refresh_status"))
    details = _mapping(record.get("details"))
    if status:
        return f"runtime refresh status: {status}"
    row_status = _text(details.get("row_status"))
    return f"runtime refresh status: {row_status}" if row_status else ""


def _status_note(record: Mapping[str, Any]) -> str:
    warnings = _list_text(record.get("warnings"))
    if warnings:
        return "; ".join(warnings)
    failed = _coerce_int(record.get("creator_detail_failed_count"), default=0, minimum=0, maximum=1000000)
    success = _coerce_int(record.get("influencer_write_success_count"), default=0, minimum=0, maximum=1000000)
    return f"creator_failed={failed}; influencer_written={success}"


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


def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


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

__all__ = [
    "PROJECTION_MAPPER_CODES",
    "PROJECTION_MAPPERS",
    "FeishuProjectionMapperError",
    "ProjectionMapper",
    "competitor_influencer_status_projection_mapper",
    "competitor_seed_projection_mapper",
    "competitor_table_projection_mapper",
    "get_projection_mapper",
    "influencer_pool_projection_mapper",
    "map_projection_record",
    "selection_table_projection_mapper",
    "selection_writeback_records",
]
