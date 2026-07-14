from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.capabilities.browser.amazon.product_page import (
    InvalidASINError,
    canonical_amazon_url,
    normalize_asin,
)


AMAZON_PRODUCT_MANUAL_PRESERVE_FIELDS = (
    "ASIN",
    "来源关键词",
    "强制刷新",
)
AMAZON_PRODUCT_PROJECTION_FIELDS = (
    "商品链接",
    "采集状态",
    "上次采集时间",
    "采集错误",
    "标题",
    "品牌",
    "类目路径",
    "卖点",
    "描述",
    "主图",
    "图库",
    "当前价格",
    "原价",
    "币种",
    "评分",
    "评论数",
    "库存状态",
    "Parent ASIN",
    "Child ASIN列表",
    "变体属性",
    "卖家",
    "配送方式",
    "Buy Box卖家",
    "Buy Box价格",
    "优惠券",
    "促销",
    "BSR排名",
    "技术参数",
    "页面ASIN",
    "字段完整度",
)
_MATERIALIZED_MEDIA_STATES = {"uploaded", "reused", "reused_in_run"}
_TERMINAL_FACT_STATUSES = {"success", "partial_success", "unavailable"}
_OFFER_PROJECTION_FIELDS = {
    "commerce.featured_offer.price_amount": "当前价格",
    "commerce.featured_offer.list_price_amount": "原价",
    "commerce.featured_offer.currency": "币种",
    "commerce.featured_offer.seller_name": "卖家",
    "commerce.featured_offer.coupon_text": "优惠券",
    "commerce.featured_offer.promotions": "促销",
}


def amazon_product_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    facts = _mapping(record.get("projection_facts")) or _mapping(record)
    source_record_id = _first_non_empty(
        record.get("source_record_id"),
        facts.get("source_record_id"),
        payload.get("source_record_id"),
    )
    if not source_record_id:
        raise ValueError("source_record_id is required for Amazon Feishu projection.")

    status = _first_non_empty(
        facts.get("collection_status"),
        record.get("collection_status"),
        "failed",
    )
    collected_at = _first_non_empty(
        facts.get("captured_at"),
        facts.get("collected_at"),
        record.get("captured_at"),
        record.get("collected_at"),
    )
    if status not in _TERMINAL_FACT_STATUSES:
        fields = {"采集状态": status}
        if collected_at:
            fields["上次采集时间"] = collected_at
        error_text = _error_text(record, facts)
        if error_text:
            fields["采集错误"] = error_text
        return _write_command(
            source_record_id=source_record_id,
            business_key=_business_key(record, facts),
            fields=fields,
            payload=payload,
        )

    requested_asin = _normalized_asin(facts.get("requested_asin"), "requested_asin")
    resolved_asin = _normalized_asin(facts.get("resolved_asin"), "resolved_asin")
    variants = _mapping(facts.get("variants"))
    parent_asin = _optional_asin(variants.get("parent_asin"))
    child_asins = _asin_list(variants.get("child_asins"))
    if requested_asin != resolved_asin and not (
        parent_asin == requested_asin and resolved_asin in child_asins
    ):
        return _write_command(
            source_record_id=source_record_id,
            business_key=f"amazon:US:{requested_asin}",
            fields={
                "采集状态": "failed",
                **({"上次采集时间": collected_at} if collected_at else {}),
                "采集错误": "identity_mismatch: resolved ASIN does not match source ASIN",
            },
            payload=payload,
        )

    evidence = _mapping(facts.get("field_evidence"))
    product = _mapping(facts.get("product"))
    commerce = _mapping(facts.get("commerce"))
    offer = _mapping(commerce.get("featured_offer"))
    fields: dict[str, Any] = {
        "商品链接": _link_value(canonical_amazon_url(requested_asin)),
        "采集状态": status,
        "采集错误": "",
        "页面ASIN": resolved_asin,
        "字段完整度": _coverage_percent(evidence),
    }
    if collected_at:
        fields["上次采集时间"] = collected_at

    _project_evidenced(fields, "标题", evidence, "product.title", product.get("title"))
    _project_evidenced(fields, "品牌", evidence, "product.brand", product.get("brand"))
    _project_evidenced(
        fields,
        "类目路径",
        evidence,
        "product.category_path",
        _join_path(product.get("category_path")),
    )
    _project_evidenced(
        fields,
        "卖点",
        evidence,
        "product.bullet_points",
        _join_lines(product.get("bullet_points")),
    )
    _project_evidenced(
        fields,
        "描述",
        evidence,
        "product.description",
        product.get("description"),
    )
    _project_evidenced(
        fields,
        "技术参数",
        evidence,
        "product.technical_details",
        _stable_json(product.get("technical_details")),
    )
    _project_evidenced(
        fields,
        "评分",
        evidence,
        "commerce.rating",
        commerce.get("rating"),
    )
    _project_evidenced(
        fields,
        "评论数",
        evidence,
        "commerce.review_count",
        commerce.get("review_count"),
    )
    availability_evidence = _evidence_status(evidence, "commerce.availability_status")
    if availability_evidence in {"observed", "explicitly_unavailable"}:
        fields["库存状态"] = _first_non_empty(
            commerce.get("availability_status"),
            "unavailable" if availability_evidence == "explicitly_unavailable" else "",
        )

    for evidence_path, field_name in _OFFER_PROJECTION_FIELDS.items():
        value = offer.get(evidence_path.rsplit(".", 1)[-1])
        if field_name == "促销":
            value = _join_lines(value)
        _project_evidenced(fields, field_name, evidence, evidence_path, value)

    _project_fulfillment(fields, evidence, offer)
    _project_buy_box(fields, evidence, offer)
    _project_evidenced(
        fields,
        "Parent ASIN",
        evidence,
        "variants.parent_asin",
        parent_asin,
    )
    _project_evidenced(
        fields,
        "Child ASIN列表",
        evidence,
        "variants.child_asins",
        "\n".join(child_asins),
    )
    _project_variants(fields, evidence, variants)
    _project_evidenced(
        fields,
        "BSR排名",
        evidence,
        "rankings",
        _rankings_text(facts.get("rankings")),
    )
    _project_media(
        fields,
        evidence,
        record.get("materialized_media_assets"),
    )

    return _write_command(
        source_record_id=source_record_id,
        business_key=f"amazon:US:{requested_asin}",
        fields=fields,
        payload=payload,
    )


def _project_evidenced(
    fields: dict[str, Any],
    field_name: str,
    evidence: Mapping[str, Any],
    evidence_path: str,
    value: Any,
) -> None:
    status = _evidence_status(evidence, evidence_path)
    if status == "observed":
        fields[field_name] = value
    elif status == "explicitly_unavailable":
        fields[field_name] = None


def _project_fulfillment(
    fields: dict[str, Any],
    evidence: Mapping[str, Any],
    offer: Mapping[str, Any],
) -> None:
    paths = (
        "commerce.featured_offer.fulfillment_channel",
        "commerce.featured_offer.delivery_text",
    )
    statuses = {_evidence_status(evidence, path) for path in paths}
    if "missing" in statuses:
        return
    if "observed" in statuses:
        channel = {
            "amazon": "Amazon",
            "merchant": "Merchant",
            "unknown": "Unknown",
        }.get(_text(offer.get("fulfillment_channel")).lower(), "")
        delivery = _text(offer.get("delivery_text"))
        fields["配送方式"] = " | ".join(item for item in (channel, delivery) if item)
    elif "explicitly_unavailable" in statuses:
        fields["配送方式"] = None


def _project_buy_box(
    fields: dict[str, Any],
    evidence: Mapping[str, Any],
    offer: Mapping[str, Any],
) -> None:
    buy_box_status = _evidence_status(evidence, "commerce.featured_offer.is_buy_box")
    if buy_box_status == "explicitly_unavailable" or (
        buy_box_status == "observed" and offer.get("is_buy_box") is False
    ):
        fields["Buy Box卖家"] = None
        fields["Buy Box价格"] = None
        return
    if buy_box_status != "observed" or offer.get("is_buy_box") is not True:
        return
    _project_evidenced(
        fields,
        "Buy Box卖家",
        evidence,
        "commerce.featured_offer.seller_name",
        offer.get("seller_name"),
    )
    _project_evidenced(
        fields,
        "Buy Box价格",
        evidence,
        "commerce.featured_offer.price_amount",
        offer.get("price_amount"),
    )


def _project_variants(
    fields: dict[str, Any],
    evidence: Mapping[str, Any],
    variants: Mapping[str, Any],
) -> None:
    attribute_status = _evidence_status(evidence, "variants.current_attributes")
    dimension_status = _evidence_status(evidence, "variants.dimensions")
    if "missing" in {attribute_status, dimension_status}:
        return
    if "observed" in {attribute_status, dimension_status}:
        fields["变体属性"] = _stable_json(
            {
                "attributes": _mapping(variants.get("current_attributes")),
                "dimensions": _mapping(variants.get("dimensions")),
            }
        )
    elif "explicitly_unavailable" in {attribute_status, dimension_status}:
        fields["变体属性"] = None


def _project_media(
    fields: dict[str, Any],
    evidence: Mapping[str, Any],
    raw_assets: Any,
) -> None:
    assets: list[dict[str, Any]] = []
    if isinstance(raw_assets, list):
        for item in raw_assets:
            if not isinstance(item, Mapping):
                continue
            if item.get("sync_state") not in _MATERIALIZED_MEDIA_STATES:
                continue
            if not _text(item.get("bucket")) or not _text(item.get("object_key")):
                continue
            assets.append(dict(item))
    if _evidence_status(evidence, "media.main_image") == "observed":
        main = next(
            (item for item in assets if item.get("media_role") == "main_image"),
            None,
        )
        if main:
            fields["主图"] = [_attachment_item(main)]
    if _evidence_status(evidence, "media.gallery_images") == "observed":
        gallery = sorted(
            (item for item in assets if item.get("media_role") == "gallery_image"),
            key=lambda item: (int(item.get("position") or 0), _text(item.get("source_url"))),
        )
        if gallery:
            fields["图库"] = [_attachment_item(item) for item in gallery]


def _attachment_item(asset: Mapping[str, Any]) -> dict[str, str]:
    return {
        key: value
        for key, value in {
            "source_url": _text(asset.get("source_url")),
            "local_path": _first_non_empty(
                asset.get("local_path"),
                asset.get("source_path"),
            ),
            "object_key": _text(asset.get("object_key")),
            "file_name": _text(asset.get("file_name")),
            "mime_type": _text(asset.get("mime_type")),
        }.items()
        if value
    }


def _write_command(
    *,
    source_record_id: str,
    business_key: str,
    fields: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    clear_fields = [
        field_name
        for field_name, value in fields.items()
        if value in (None, "", [])
    ]
    return {
        "op": "update",
        "record_id": source_record_id,
        "business_entity_key": business_key,
        "update_excluded_fields": list(AMAZON_PRODUCT_MANUAL_PRESERVE_FIELDS),
        "update_replace_fields": ["主图", "图库"],
        "clear_fields": clear_fields,
        "fields": dict(fields),
        "source_context": {
            "source_record_id": source_record_id,
            "workflow_code": _text(payload.get("workflow_code")),
            "stage_code": _text(payload.get("stage_code")),
            "projection_type": "amazon_product_projection_mapper",
        },
    }


def _business_key(record: Mapping[str, Any], facts: Mapping[str, Any]) -> str:
    raw_asin = _first_non_empty(facts.get("requested_asin"), record.get("requested_asin"))
    try:
        return f"amazon:US:{normalize_asin(raw_asin)}"
    except InvalidASINError:
        return ""


def _error_text(record: Mapping[str, Any], facts: Mapping[str, Any]) -> str:
    code = _first_non_empty(record.get("error_code"), facts.get("error_code"))
    message = _first_non_empty(record.get("error_message"), facts.get("error_message"))
    text = ": ".join(item for item in (code, message) if item)
    return text[:1000]


def _evidence_status(evidence: Mapping[str, Any], path: str) -> str:
    item = evidence.get(path)
    return _text(item.get("status")) if isinstance(item, Mapping) else "missing"


def _coverage_percent(evidence: Mapping[str, Any]) -> float:
    statuses = [
        _text(item.get("status"))
        for item in evidence.values()
        if isinstance(item, Mapping)
    ]
    if not statuses:
        return 0.0
    covered = sum(
        1 for status in statuses if status in {"observed", "explicitly_unavailable"}
    )
    return round((covered / len(statuses)) * 100.0, 2)


def _rankings_text(value: Any) -> str:
    lines: list[str] = []
    if not isinstance(value, list):
        return ""
    for item in value:
        if not isinstance(item, Mapping):
            continue
        rank = item.get("rank")
        category = _join_path(item.get("category_path")) or _text(
            item.get("category_name")
        )
        if rank not in (None, "") and category:
            lines.append(f"#{rank} - {category}")
    return "\n".join(lines)


def _stable_json(value: Any) -> str:
    if not isinstance(value, (Mapping, list)):
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _join_path(value: Any) -> str:
    return " > ".join(_text_list(value))


def _join_lines(value: Any) -> str:
    return "\n".join(_text_list(value))


def _text_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [_text(item) for item in value if _text(item)]
    item = _text(value)
    return [item] if item else []


def _asin_list(value: Any) -> list[str]:
    result: list[str] = []
    if not isinstance(value, list):
        return result
    for item in value:
        try:
            asin = normalize_asin(item)
        except InvalidASINError:
            continue
        if asin not in result:
            result.append(asin)
    return result


def _optional_asin(value: Any) -> str:
    if value in (None, ""):
        return ""
    return _normalized_asin(value, "ASIN")


def _normalized_asin(value: Any, name: str) -> str:
    try:
        return normalize_asin(value)
    except InvalidASINError as exc:
        raise ValueError(f"{name} is invalid.") from exc


def _link_value(url: str) -> dict[str, str]:
    return {"text": url, "link": url}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first_non_empty(*values: Any) -> str:
    for value in values:
        item = _text(value)
        if item:
            return item
    return ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


__all__ = [
    "AMAZON_PRODUCT_MANUAL_PRESERVE_FIELDS",
    "AMAZON_PRODUCT_PROJECTION_FIELDS",
    "amazon_product_projection_mapper",
]
