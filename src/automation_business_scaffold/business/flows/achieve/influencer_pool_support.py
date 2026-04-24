from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import requests

from automation_business_scaffold.infrastructure.feishu.api import (
    FeishuAPIError,
    FeishuBitableClient,
    parse_table_url,
)
from automation_business_scaffold.infrastructure.facts.tk_fact_ingestion_service import TKFactIngestionService


FEISHU_FIELD_TYPE_TEXT = 1
FEISHU_FIELD_TYPE_SINGLE_SELECT = 3
FEISHU_FIELD_TYPE_MULTI_SELECT = 4
FEISHU_FIELD_TYPE_DATE_TIME = 5
FEISHU_FIELD_TYPE_ATTACHMENT = 17
FEISHU_FIELD_TYPE_FORMULA = 20

DEFAULT_INFLUENCER_ID_FIELD_NAME = "达人ID"
DEFAULT_COMPETITOR_IMAGE_FIELD_NAME = "图片"
DEFAULT_COMPETITOR_HOLIDAY_FIELD_NAME = "节日"
DEFAULT_COMPETITOR_SALES_FIELD_NAME = "商品总销量"
DEFAULT_COMPETITOR_PRODUCT_ID_FIELD_NAME = "SKU-ID"
DEFAULT_COMPETITOR_STATUS_FIELD_NAME = "商品状态"
DEFAULT_COMPETITOR_SYNC_STATUS_FIELD_NAME = "达人查找状态"

DEFAULT_INFLUENCER_IMAGE_FIELD_NAME = "带货商品图"
DEFAULT_INFLUENCER_AVATAR_FIELD_NAME = "达人头像"
DEFAULT_INFLUENCER_HOLIDAY_FIELD_NAME = "关联节日"
DEFAULT_INFLUENCER_SALES_FIELD_NAME = "关联商品销量"
DEFAULT_INFLUENCER_FOLLOWER_FIELD_NAME = "粉丝数"
DEFAULT_INFLUENCER_28D_VIDEOS_FIELD_NAME = "28天视频数"
DEFAULT_INFLUENCER_VIDEO_GMV_FIELD_NAME = "带货视频 GMV"
DEFAULT_INFLUENCER_LIVE_GMV_FIELD_NAME = "带货直播 GMV"
DEFAULT_INFLUENCER_SHOP_FIELD_NAME = "合作店铺"
DEFAULT_INFLUENCER_PRODUCT_COUNT_FIELD_NAME = "合作商品数"
DEFAULT_INFLUENCER_CONTACT_FIELD_NAME = "达人联系方式"
DEFAULT_INFLUENCER_RECORD_TIME_FIELD_NAME = "记录时间"
DEFAULT_INFLUENCER_DUPLICATE_CHECK_FIELD_NAME = "检查达人名称是否重复"

DEFAULT_EXCLUDED_WRITABLE_FIELD_NAMES = {DEFAULT_INFLUENCER_DUPLICATE_CHECK_FIELD_NAME}
CONTACT_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
CONTACT_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
CONTACT_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")


@dataclass
class InfluencerTraceError(RuntimeError):
    error: str
    stage: str = ""
    field: str = ""
    url: str = ""
    influencer_id: str = ""
    cause_type: str = ""
    status_code: int | None = None

    def __post_init__(self) -> None:
        super().__init__(self.error)

    def to_dict(self) -> dict[str, Any]:
        return {
            "influencer_id": str(self.influencer_id or "").strip(),
            "stage": str(self.stage or "").strip(),
            "field": str(self.field or "").strip(),
            "url": str(self.url or "").strip(),
            "error": str(self.error or "").strip(),
            "cause_type": str(self.cause_type or "").strip(),
            "status_code": self.status_code,
        }


def load_table_schema(client: FeishuBitableClient, table_url: str) -> dict[str, Any]:
    table_meta = parse_table_url(table_url)
    fields = client.list_all_fields(table_meta["app_token"], table_meta["table_id"], page_size=100)
    return {
        "app_token": table_meta["app_token"],
        "table_id": table_meta["table_id"],
        "view_id": table_meta.get("view_id", ""),
        "fields": fields,
        "field_name_to_meta": build_field_name_to_meta(fields),
        "field_id_to_meta": _build_field_id_to_meta(fields),
    }


def build_field_name_to_meta(fields: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    field_name_to_meta: dict[str, dict[str, Any]] = {}
    for raw_field in fields:
        if not isinstance(raw_field, Mapping):
            continue
        field_name = str(raw_field.get("field_name") or raw_field.get("name") or "").strip()
        if not field_name or field_name in field_name_to_meta:
            continue
        field_name_to_meta[field_name] = _normalize_field_meta(raw_field)
    return field_name_to_meta


def build_multi_select_option_name_map(field_meta: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(field_meta, Mapping):
        return {}
    property_payload = field_meta.get("property")
    if not isinstance(property_payload, Mapping):
        return {}
    options = property_payload.get("options")
    if not isinstance(options, Sequence):
        return {}

    option_name_map: dict[str, dict[str, Any]] = {}
    for option in options:
        if not isinstance(option, Mapping):
            continue
        option_name = str(option.get("name") or "").strip()
        if not option_name or option_name in option_name_map:
            continue
        option_name_map[option_name] = dict(option)
    return option_name_map


def format_influencer_contacts(raw_contact_payload: Any) -> str:
    contact_items = _extract_contact_items(raw_contact_payload)
    lines: list[str] = []
    seen_names: set[str] = set()

    for item in contact_items:
        formatted = _format_influencer_contact_item(item)
        if not formatted:
            continue
        channel_name = formatted.split(":", 1)[0]
        if not channel_name or channel_name in seen_names:
            continue
        seen_names.add(channel_name)
        lines.append(formatted)

    return "\n".join(lines)


def format_first_influencer_contact(raw_contact_payload: Any) -> str:
    for item in _extract_contact_items(raw_contact_payload):
        formatted = _format_influencer_contact_item(item)
        if formatted:
            return formatted
    return ""


def _format_influencer_contact_item(item: Any) -> str:
    if not isinstance(item, Mapping):
        return ""
    if not _is_truthy(item.get("has")):
        return ""

    channel_name = str(item.get("name") or item.get("channel_name") or item.get("id") or "").strip()
    if not channel_name:
        return ""
    channel_key = channel_name.strip().lower()
    contact_value = _extract_contact_value_for_channel(item, channel_key=channel_key)
    if not contact_value:
        return ""
    return f"{channel_name}:{contact_value}"


def _extract_contact_value_for_channel(item: Mapping[str, Any], *, channel_key: str) -> str:
    if channel_key == "email":
        return _extract_first_email(_first_non_empty(item.get("id"), item.get("channel_name"), item.get("link")))

    if channel_key == "bio":
        raw_text = _first_non_empty(item.get("link"), item.get("channel_name"), item.get("value"), item.get("text"))
        return _extract_contactable_text(raw_text)

    for key in ("link", "id", "channel_name", "value", "text"):
        value = str(item.get(key) or "").strip()
        if not value or value.lower() == channel_key:
            continue
        return value
    return ""


def _extract_contactable_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    url_match = CONTACT_URL_RE.search(text)
    if url_match:
        return url_match.group(0).rstrip(".,;")
    email = _extract_first_email(text)
    if email:
        return email
    phone_match = CONTACT_PHONE_RE.search(text)
    if phone_match:
        return phone_match.group(0).strip()
    return ""


def _extract_first_email(value: Any) -> str:
    match = CONTACT_EMAIL_RE.search(str(value or ""))
    return match.group(0) if match else ""


def prepare_remote_attachment_field(
    attachment_value: Any,
    *,
    client: FeishuBitableClient,
    parent_node: str,
    session: requests.Session | None = None,
    prefer_passthrough: bool = True,
    trace_context: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    normalized_items = _normalize_attachment_items(attachment_value)
    if not normalized_items:
        return []

    trace_context = dict(trace_context or {})
    trace_influencer_id = str(trace_context.get("influencer_id") or "").strip()
    trace_field = str(trace_context.get("field") or "").strip()
    trace_stage = str(trace_context.get("stage") or "attachment").strip() or "attachment"

    uploaded_items: list[dict[str, str]] = []
    seen_tokens: set[str] = set()
    download_session = session or client.session

    for item in normalized_items:
        if not isinstance(item, Mapping):
            item = {"value": item}
        local_path_text = _first_non_empty(item.get("path"), item.get("local_path"))
        if local_path_text:
            local_path = Path(local_path_text)
            if not local_path.exists():
                raise InfluencerTraceError(
                    error=f"Attachment file does not exist: {local_path}",
                    stage=f"{trace_stage}.local_file",
                    field=trace_field,
                    url=str(local_path),
                    influencer_id=trace_influencer_id,
                    cause_type="FileNotFoundError",
                )
            try:
                new_token = client.upload_media(
                    file_name=_infer_attachment_file_name(item, download_url=str(local_path)),
                    file_data=local_path.read_bytes(),
                    parent_node=parent_node,
                )
            except Exception as exc:
                raise InfluencerTraceError(
                    error=str(exc),
                    stage=f"{trace_stage}.upload",
                    field=trace_field,
                    url=str(local_path),
                    influencer_id=trace_influencer_id,
                    cause_type=type(exc).__name__,
                ) from exc
            if new_token and new_token not in seen_tokens:
                uploaded_items.append({"file_token": new_token})
                seen_tokens.add(new_token)
            continue
        file_token = str(item.get("file_token") or "").strip()
        if file_token and prefer_passthrough:
            if file_token not in seen_tokens:
                uploaded_items.append({"file_token": file_token})
                seen_tokens.add(file_token)
            continue

        download_url = _first_non_empty(
            item.get("tmp_url"),
            item.get("url"),
            item.get("link"),
            item.get("download_url"),
        )
        if not download_url and file_token:
            if file_token not in seen_tokens:
                uploaded_items.append({"file_token": file_token})
                seen_tokens.add(file_token)
            continue

        file_name = _infer_attachment_file_name(item, download_url=download_url)
        try:
            file_bytes = _download_remote_attachment_bytes(download_url, session=download_session)
        except Exception as exc:
                raise InfluencerTraceError(
                    error=str(exc),
                    stage=f"{trace_stage}.download",
                    field=trace_field,
                    url=download_url,
                    influencer_id=trace_influencer_id,
                    cause_type=type(exc).__name__,
                    status_code=getattr(exc, "status", None),
                ) from exc
        try:
            new_token = client.upload_media(
                file_name=file_name,
                file_data=file_bytes,
                parent_node=parent_node,
            )
        except Exception as exc:
                raise InfluencerTraceError(
                    error=str(exc),
                    stage=f"{trace_stage}.upload",
                    field=trace_field,
                    url=download_url,
                    influencer_id=trace_influencer_id,
                    cause_type=type(exc).__name__,
                    status_code=getattr(exc, "status", None),
                ) from exc
        if new_token and new_token not in seen_tokens:
            uploaded_items.append({"file_token": new_token})
            seen_tokens.add(new_token)

    return uploaded_items


def build_influencer_record_index(
    records: Sequence[Mapping[str, Any]] | None = None,
    *,
    influencer_id_field_name: str = DEFAULT_INFLUENCER_ID_FIELD_NAME,
) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}

    for raw_record in records or []:
        state = _normalize_influencer_state_from_record(
            raw_record,
            influencer_id_field_name=influencer_id_field_name,
        )
        if not state:
            continue
        influencer_id = str(state.get("influencer_id", "") or "").strip()
        if not influencer_id:
            continue
        index[influencer_id] = merge_influencer_facts(index.get(influencer_id), state)

    return index


def merge_influencer_facts(
    existing_state: Mapping[str, Any] | None,
    incoming_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    merged = _normalize_influencer_state(existing_state)
    incoming = _normalize_influencer_state(incoming_state)
    if not incoming:
        return merged

    for key in (
        "influencer_id",
        "record_id",
        "target_record_id",
        "table_url",
        "source_key",
    ):
        value = str(incoming.get(key, "") or "").strip()
        if value and not str(merged.get(key, "") or "").strip():
            merged[key] = value

    merged["source_product_ids"] = _merge_unique_values(
        merged.get("source_product_ids"),
        incoming.get("source_product_ids"),
    )

    merged["holiday_names"] = _merge_unique_values(
        merged.get("holiday_names"),
        incoming.get("holiday_names"),
    )
    merged["cooperation_shop_names"] = _merge_unique_values(
        merged.get("cooperation_shop_names"),
        incoming.get("cooperation_shop_names"),
    )

    source_product_sales_by_id = dict(merged.get("source_product_sales_by_id") or {})
    incoming_sales_by_id = incoming.get("source_product_sales_by_id")
    if isinstance(incoming_sales_by_id, Mapping):
        for product_id, raw_value in incoming_sales_by_id.items():
            product_id = str(product_id or "").strip()
            if not product_id:
                continue
            if product_id in source_product_sales_by_id:
                continue
            source_product_sales_by_id[product_id] = _coerce_number(raw_value)
    merged["source_product_sales_by_id"] = source_product_sales_by_id

    source_product_image_refs_by_id = dict(merged.get("source_product_image_refs_by_id") or {})
    incoming_image_refs_by_id = incoming.get("source_product_image_refs_by_id")
    if isinstance(incoming_image_refs_by_id, Mapping):
        for product_id, raw_refs in incoming_image_refs_by_id.items():
            product_id = str(product_id or "").strip()
            if not product_id:
                continue
            if product_id in source_product_image_refs_by_id:
                continue
            refs = _normalize_attachment_items(raw_refs)
            if refs:
                source_product_image_refs_by_id[product_id] = [dict(ref) for ref in refs]
    merged["source_product_image_refs_by_id"] = source_product_image_refs_by_id

    for key in (
        "avatar",
        "follower_count",
        "aweme_28_count",
        "video_sale_amount",
        "live_sale_amount",
        "contact_text",
        "record_time_ms",
    ):
        value = incoming.get(key)
        if _has_meaningful_value(value):
            merged[key] = value

    merged["source_product_count"] = len(merged.get("source_product_ids") or [])
    merged["total_source_product_sales"] = sum(
        _coerce_number(value) for value in (merged.get("source_product_sales_by_id") or {}).values()
    )
    merged["holiday_count"] = len(merged.get("holiday_names") or [])
    merged["cooperation_shop_count"] = len(merged.get("cooperation_shop_names") or [])
    return merged


def influencer_state_has_source_product(
    state: Mapping[str, Any] | None,
    product_id: str,
) -> bool:
    normalized_product_id = str(product_id or "").strip()
    if not normalized_product_id or not isinstance(state, Mapping):
        return False
    normalized_state = _normalize_influencer_state(state)
    return normalized_product_id in {
        str(candidate or "").strip()
        for candidate in normalized_state.get("source_product_ids") or []
    }


def build_influencer_write_fields(
    *,
    target_schema: Mapping[str, Any],
    influencer_state: Mapping[str, Any],
    client: FeishuBitableClient,
    parent_node: str,
    session: requests.Session | None = None,
    prefer_passthrough: bool = True,
    non_blocking_failures: list[dict[str, Any]] | None = None,
    sleep_factory: Any = time.sleep,
) -> dict[str, Any]:
    field_name_to_meta = _resolve_field_name_to_meta(target_schema)
    if not field_name_to_meta:
        raise ValueError("target_schema does not contain field metadata")

    normalized_state = _normalize_influencer_state(influencer_state)
    writable_fields: dict[str, Any] = {}

    influencer_id = str(normalized_state.get("influencer_id", "") or "").strip()
    if influencer_id and _is_writable_field(field_name_to_meta, DEFAULT_INFLUENCER_ID_FIELD_NAME):
        writable_fields[DEFAULT_INFLUENCER_ID_FIELD_NAME] = influencer_id

    image_tokens = _build_influencer_image_tokens(
        normalized_state,
        client=client,
        parent_node=parent_node,
        session=session,
        prefer_passthrough=prefer_passthrough,
        influencer_id=influencer_id,
    )
    if image_tokens and _is_writable_field(field_name_to_meta, DEFAULT_INFLUENCER_IMAGE_FIELD_NAME):
        writable_fields[DEFAULT_INFLUENCER_IMAGE_FIELD_NAME] = image_tokens

    avatar_value = normalized_state.get("avatar")
    if _has_meaningful_value(avatar_value) and _is_writable_field(field_name_to_meta, DEFAULT_INFLUENCER_AVATAR_FIELD_NAME):
        avatar_tokens = _build_avatar_attachment_tokens(
            avatar_value,
            client=client,
            parent_node=parent_node,
            session=session,
            prefer_passthrough=prefer_passthrough,
            influencer_id=influencer_id,
            non_blocking_failures=non_blocking_failures,
            sleep_factory=sleep_factory,
        )
        if avatar_tokens:
            writable_fields[DEFAULT_INFLUENCER_AVATAR_FIELD_NAME] = avatar_tokens

    holiday_values = _normalize_multi_select_names(
        normalized_state.get("holiday_names"),
        field_name_to_meta.get(DEFAULT_INFLUENCER_HOLIDAY_FIELD_NAME),
    )
    if holiday_values and _is_writable_field(field_name_to_meta, DEFAULT_INFLUENCER_HOLIDAY_FIELD_NAME):
        writable_fields[DEFAULT_INFLUENCER_HOLIDAY_FIELD_NAME] = holiday_values

    shop_values = _normalize_multi_select_names(
        normalized_state.get("cooperation_shop_names"),
        field_name_to_meta.get(DEFAULT_INFLUENCER_SHOP_FIELD_NAME),
    )
    if shop_values and _is_writable_field(field_name_to_meta, DEFAULT_INFLUENCER_SHOP_FIELD_NAME):
        writable_fields[DEFAULT_INFLUENCER_SHOP_FIELD_NAME] = shop_values

    wan_unit_display_fields = {
        DEFAULT_INFLUENCER_FOLLOWER_FIELD_NAME,
        DEFAULT_INFLUENCER_VIDEO_GMV_FIELD_NAME,
        DEFAULT_INFLUENCER_LIVE_GMV_FIELD_NAME,
    }
    for field_name, state_key in (
        (DEFAULT_INFLUENCER_SALES_FIELD_NAME, "total_source_product_sales"),
        (DEFAULT_INFLUENCER_FOLLOWER_FIELD_NAME, "follower_count"),
        (DEFAULT_INFLUENCER_28D_VIDEOS_FIELD_NAME, "aweme_28_count"),
        (DEFAULT_INFLUENCER_VIDEO_GMV_FIELD_NAME, "video_sale_amount"),
        (DEFAULT_INFLUENCER_LIVE_GMV_FIELD_NAME, "live_sale_amount"),
        (DEFAULT_INFLUENCER_CONTACT_FIELD_NAME, "contact_text"),
    ):
        value = normalized_state.get(state_key)
        if not _has_meaningful_value(value):
            continue
        if not _is_writable_field(field_name_to_meta, field_name):
            continue
        if field_name in wan_unit_display_fields:
            writable_fields[field_name] = _format_w_unit_display_field(value)
        else:
            writable_fields[field_name] = _stringify_scalar_field(value)

    record_time_ms = normalized_state.get("record_time_ms")
    if _is_writable_field(field_name_to_meta, DEFAULT_INFLUENCER_RECORD_TIME_FIELD_NAME):
        writable_fields[DEFAULT_INFLUENCER_RECORD_TIME_FIELD_NAME] = _build_record_time_timestamp_ms(record_time_ms)

    return writable_fields


def build_influencer_snapshot_facts(influencer_state: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized = _normalize_influencer_state(influencer_state)
    if not normalized:
        return {}
    return {
        "influencer_id": str(normalized.get("influencer_id", "") or "").strip(),
        "uid": str(normalized.get("uid", "") or "").strip(),
        "unique_id": str(normalized.get("unique_id", "") or "").strip(),
        "nickname": str(normalized.get("nickname", "") or "").strip(),
        "record_id": str(normalized.get("record_id", "") or "").strip(),
        "target_record_id": str(normalized.get("target_record_id", "") or "").strip(),
        "table_url": str(normalized.get("table_url", "") or "").strip(),
        "source_key": str(normalized.get("source_key", "") or "").strip(),
        "source_product_ids": list(normalized.get("source_product_ids") or []),
        "source_product_sales_by_id": dict(normalized.get("source_product_sales_by_id") or {}),
        "source_product_image_refs_by_id": dict(normalized.get("source_product_image_refs_by_id") or {}),
        "holiday_names": list(normalized.get("holiday_names") or []),
        "cooperation_shop_names": list(normalized.get("cooperation_shop_names") or []),
        "avatar": normalized.get("avatar", ""),
        "follower_count": _stringify_scalar_field(normalized.get("follower_count")),
        "aweme_28_count": _stringify_scalar_field(normalized.get("aweme_28_count")),
        "video_sale_amount": _stringify_scalar_field(normalized.get("video_sale_amount")),
        "live_sale_amount": _stringify_scalar_field(normalized.get("live_sale_amount")),
        "contact_text": str(normalized.get("contact_text", "") or "").strip(),
        "record_time_ms": _coerce_datetime_timestamp_ms(normalized.get("record_time_ms")),
        "source_product_count": int(normalized.get("source_product_count") or 0),
        "total_source_product_sales": _coerce_number(normalized.get("total_source_product_sales")),
        "holiday_count": int(normalized.get("holiday_count") or 0),
        "cooperation_shop_count": int(normalized.get("cooperation_shop_count") or 0),
    }


def persist_influencer_fact_bundle(
    *,
    store: Any,
    execution: Any,
    influencer_state: Mapping[str, Any],
    table_url: str,
    target_record_id: str = "",
    source_key: str = "",
) -> dict[str, Any]:
    normalized_state = _normalize_influencer_state(influencer_state)
    influencer_id = str(normalized_state.get("influencer_id", "") or "").strip()
    if not influencer_id:
        return {}

    facts = build_influencer_snapshot_facts(
        {**normalized_state, "target_record_id": target_record_id, "table_url": table_url}
    )
    product_ids = [str(value or "").strip() for value in normalized_state.get("source_product_ids") or []]
    sales_by_product = dict(normalized_state.get("source_product_sales_by_id") or {})
    image_refs_by_product = dict(normalized_state.get("source_product_image_refs_by_id") or {})
    holiday_names = list(normalized_state.get("holiday_names") or [])
    primary_holiday = str(holiday_names[0] if holiday_names else "")
    creator_key = (
        f"creator_id:{influencer_id}"
        if influencer_id
        else _first_non_empty(str(normalized_state.get("uid", "") or ""))
    )
    products: list[dict[str, Any]] = []
    creator_products: list[dict[str, Any]] = []
    media_assets: list[dict[str, Any]] = []
    for product_id in product_ids:
        if not product_id:
            continue
        products.append(
            {
                "product_id": product_id,
                "holiday": primary_holiday,
                "source_platform": "fastmoss",
                "facts": {"source": "influencer_pool", "influencer_id": influencer_id},
            }
        )
        creator_products.append(
            {
                "creator_key": creator_key,
                "creator_id": influencer_id,
                "product_id": product_id,
                "source_record_id": str(source_key or ""),
                "target_record_id": target_record_id,
                "holiday_name": primary_holiday,
                "sold_count": sales_by_product.get(product_id, 0),
                "metadata": {"table_url": table_url},
            }
        )
        for image_ref in _normalize_attachment_items(image_refs_by_product.get(product_id)):
            if not isinstance(image_ref, Mapping):
                continue
            media_assets.append(
                {
                    "entity_type": "product",
                    "entity_external_id": product_id,
                    "media_role": "source_product_image",
                    "source_url": _first_non_empty(
                        image_ref.get("tmp_url"),
                        image_ref.get("url"),
                        image_ref.get("link"),
                        image_ref.get("download_url"),
                    ),
                    "file_token": _first_non_empty(image_ref.get("file_token")),
                    "local_path": _first_non_empty(image_ref.get("path"), image_ref.get("local_path")),
                    "source_platform": "feishu",
                    "metadata": {"influencer_id": influencer_id},
                }
            )

    shops: list[dict[str, Any]] = []
    shop_creators: list[dict[str, Any]] = []
    for shop_name in normalized_state.get("cooperation_shop_names") or []:
        shop_name = str(shop_name or "").strip()
        if not shop_name:
            continue
        shops.append(
            {
                "shop_name": shop_name,
                "source_platform": "fastmoss",
                "facts": {"source": "influencer_pool", "influencer_id": influencer_id},
            }
        )
        shop_creators.append(
            {
                "shop_name": shop_name,
                "creator_key": creator_key,
                "creator_id": influencer_id,
                "metadata": {"table_url": table_url},
            }
        )
    if normalized_state.get("avatar"):
        media_assets.append(
            {
                "entity_type": "creator",
                "entity_external_id": creator_key,
                "media_role": "creator_avatar",
                "source_url": _first_non_empty(normalized_state.get("avatar")),
                "source_platform": "fastmoss",
                "metadata": {"influencer_id": influencer_id},
            }
        )

    return TKFactIngestionService(runtime_store=store).ingest_api_response(
        source_platform="fastmoss",
        source_endpoint="influencer_pool.author_result",
        request_params={"influencer_id": influencer_id, "target_record_id": target_record_id},
        response_payload=facts,
        creators=[
            {
                "creator_id": influencer_id,
                "uid": str(normalized_state.get("uid", "") or ""),
                "unique_id": str(normalized_state.get("unique_id", "") or influencer_id),
                "nickname": str(normalized_state.get("nickname", "") or ""),
                "source_platform": "fastmoss",
                "facts": facts,
            }
        ],
        products=products,
        shops=shops,
        media_assets=media_assets,
        relations={
            "creator_products": creator_products,
            "shop_creators": shop_creators,
        },
        raw_entity_links=[
            {
                "entity_type": "creator",
                "entity_external_id": creator_key,
                "link_role": "primary_creator",
            }
        ],
        execution=execution,
    )


def _build_field_id_to_meta(fields: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    field_id_to_meta: dict[str, dict[str, Any]] = {}
    for raw_field in fields:
        if not isinstance(raw_field, Mapping):
            continue
        field_id = str(raw_field.get("field_id") or "").strip()
        if not field_id or field_id in field_id_to_meta:
            continue
        field_id_to_meta[field_id] = _normalize_field_meta(raw_field)
    return field_id_to_meta


def _normalize_field_meta(raw_field: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "field_id": str(raw_field.get("field_id") or "").strip(),
        "field_name": str(raw_field.get("field_name") or raw_field.get("name") or "").strip(),
        "type": int(raw_field.get("type") or 0),
        "ui_type": str(raw_field.get("ui_type") or "").strip(),
        "is_primary": bool(raw_field.get("is_primary") or False),
        "is_extend": bool(raw_field.get("is_extend") or False),
        "is_synced": bool(raw_field.get("is_synced") or False),
        "property": copy.deepcopy(raw_field.get("property")) if isinstance(raw_field.get("property"), Mapping) else raw_field.get("property"),
        "raw": copy.deepcopy(dict(raw_field)),
    }


def _resolve_field_name_to_meta(target_schema: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if "field_name_to_meta" in target_schema and isinstance(target_schema["field_name_to_meta"], Mapping):
        return {
            str(field_name): dict(meta)
            for field_name, meta in target_schema["field_name_to_meta"].items()
            if isinstance(field_name, str) and isinstance(meta, Mapping)
        }
    if all(key in target_schema for key in ("fields", "app_token", "table_id")):
        fields = target_schema.get("fields")
        if isinstance(fields, Sequence):
            return build_field_name_to_meta(fields)  # type: ignore[arg-type]
    if all(key in target_schema for key in ("app_token", "table_id")):
        fields = target_schema.get("fields")
        if isinstance(fields, Sequence):
            return build_field_name_to_meta(fields)  # type: ignore[arg-type]
    return {}


def _is_writable_field(field_name_to_meta: Mapping[str, Mapping[str, Any]], field_name: str) -> bool:
    if field_name in DEFAULT_EXCLUDED_WRITABLE_FIELD_NAMES:
        return False
    meta = field_name_to_meta.get(field_name)
    if not isinstance(meta, Mapping):
        return False
    return int(meta.get("type") or 0) != FEISHU_FIELD_TYPE_FORMULA


def _normalize_multi_select_names(
    value: Any,
    field_meta: Mapping[str, Any] | None,
) -> list[str]:
    raw_names = _normalize_name_list(value)
    if not raw_names:
        return []
    option_name_map = build_multi_select_option_name_map(field_meta)
    if not option_name_map:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for name in raw_names:
        if name not in option_name_map or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def _build_influencer_image_tokens(
    influencer_state: Mapping[str, Any],
    *,
    client: FeishuBitableClient,
    parent_node: str,
    session: requests.Session | None,
    prefer_passthrough: bool,
    influencer_id: str,
) -> list[dict[str, str]]:
    image_refs_by_id = influencer_state.get("source_product_image_refs_by_id")
    if not isinstance(image_refs_by_id, Mapping):
        return []

    ordered_refs: list[Any] = []
    seen_product_ids: set[str] = set()
    for product_id, refs in image_refs_by_id.items():
        product_id = str(product_id or "").strip()
        if not product_id or product_id in seen_product_ids:
            continue
        seen_product_ids.add(product_id)
        if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes, bytearray)):
            ordered_refs.extend(refs)
        else:
            ordered_refs.append(refs)

    return prepare_remote_attachment_field(
        ordered_refs,
        client=client,
        parent_node=parent_node,
        session=session,
        prefer_passthrough=prefer_passthrough,
        trace_context={
            "influencer_id": influencer_id,
            "field": DEFAULT_INFLUENCER_IMAGE_FIELD_NAME,
            "stage": "feishu.attachment",
        },
    )


def _build_avatar_attachment_tokens(
    avatar_value: Any,
    *,
    client: FeishuBitableClient,
    parent_node: str,
    session: requests.Session | None,
    prefer_passthrough: bool,
    influencer_id: str,
    non_blocking_failures: list[dict[str, Any]] | None,
    sleep_factory: Any,
) -> list[dict[str, str]]:
    trace_context = {
        "influencer_id": influencer_id,
        "field": DEFAULT_INFLUENCER_AVATAR_FIELD_NAME,
        "stage": "feishu.attachment",
    }
    try:
        return prepare_remote_attachment_field(
            avatar_value,
            client=client,
            parent_node=parent_node,
            session=session,
            prefer_passthrough=prefer_passthrough,
            trace_context=trace_context,
        )
    except InfluencerTraceError as exc:
        if not _is_skippable_avatar_download_404(exc):
            raise
        sleep_factory(3.0)
        try:
            return prepare_remote_attachment_field(
                avatar_value,
                client=client,
                parent_node=parent_node,
                session=session,
                prefer_passthrough=prefer_passthrough,
                trace_context=trace_context,
            )
        except InfluencerTraceError as retry_exc:
            if not _is_skippable_avatar_download_404(retry_exc):
                raise
            if non_blocking_failures is not None:
                non_blocking_failures.append(
                    {
                        "influencer_id": influencer_id,
                        "stage": retry_exc.stage,
                        "field": retry_exc.field,
                        "url": retry_exc.url,
                        "error": retry_exc.error,
                        "error_type": retry_exc.cause_type or "trace_error",
                        "status_code": retry_exc.status_code,
                        "resolution": "skipped_non_blocking",
                    }
                )
            return []


def _is_skippable_avatar_download_404(exc: InfluencerTraceError) -> bool:
    error_text = str(exc.error or "")
    return (
        str(exc.field or "").strip() == DEFAULT_INFLUENCER_AVATAR_FIELD_NAME
        and str(exc.stage or "").strip().endswith(".download")
        and (
            int(exc.status_code or 0) == 404
            or "status=404" in error_text
            or "Download failed: 404" in error_text
        )
    )


def _normalize_influencer_state(record: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        return {
            "source_product_ids": [],
            "source_product_sales_by_id": {},
            "source_product_image_refs_by_id": {},
            "holiday_names": [],
            "cooperation_shop_names": [],
        }

    normalized: dict[str, Any] = {
        "influencer_id": _first_non_empty(
            record.get("influencer_id"),
            record.get("达人ID"),
            record.get("unique_id"),
        ),
        "uid": _first_non_empty(record.get("uid")),
        "unique_id": _first_non_empty(
            record.get("unique_id"),
            record.get("influencer_id"),
            record.get("达人ID"),
        ),
        "nickname": _first_non_empty(record.get("nickname"), record.get("nick_name"), record.get("达人名称")),
        "record_id": _first_non_empty(record.get("record_id"), record.get("id")),
        "target_record_id": _first_non_empty(record.get("target_record_id")),
        "table_url": _first_non_empty(record.get("table_url")),
        "source_key": _first_non_empty(record.get("source_key")),
        "source_product_ids": _normalize_name_list(record.get("source_product_ids")),
        "source_product_sales_by_id": _normalize_mapping_of_numbers(record.get("source_product_sales_by_id")),
        "source_product_image_refs_by_id": _normalize_mapping_of_attachments(record.get("source_product_image_refs_by_id")),
        "holiday_names": _normalize_name_list(record.get("holiday_names")),
        "cooperation_shop_names": _normalize_name_list(record.get("cooperation_shop_names")),
        "avatar": record.get("avatar"),
        "follower_count": record.get("follower_count"),
        "aweme_28_count": record.get("aweme_28_count"),
        "video_sale_amount": record.get("video_sale_amount"),
        "live_sale_amount": record.get("live_sale_amount"),
        "contact_text": _first_non_empty(record.get("contact_text"), record.get("达人联系方式")),
        "record_time_ms": _coerce_datetime_timestamp_ms(
            record.get("record_time_ms") or record.get("record_time")
        ),
        "source_product_count": _coerce_int(record.get("source_product_count"), default=0),
        "total_source_product_sales": _coerce_number(record.get("total_source_product_sales")),
        "holiday_count": _coerce_int(record.get("holiday_count"), default=0),
        "cooperation_shop_count": _coerce_int(record.get("cooperation_shop_count"), default=0),
    }
    normalized["source_product_count"] = len(normalized["source_product_ids"])
    if not normalized["total_source_product_sales"] and normalized["source_product_sales_by_id"]:
        normalized["total_source_product_sales"] = sum(normalized["source_product_sales_by_id"].values())
    normalized["holiday_count"] = len(normalized["holiday_names"])
    normalized["cooperation_shop_count"] = len(normalized["cooperation_shop_names"])
    return normalized


def _normalize_influencer_state_from_record(
    record: Mapping[str, Any],
    *,
    influencer_id_field_name: str,
) -> dict[str, Any]:
    fields = record.get("fields")
    if not isinstance(fields, Mapping):
        fields = record

    state = _normalize_influencer_state(
        {
            **dict(fields),
            "influencer_id": _first_non_empty(
                fields.get(influencer_id_field_name),
                record.get("influencer_id"),
            ),
            "record_id": _first_non_empty(record.get("record_id")),
            "target_record_id": _first_non_empty(record.get("record_id")),
        }
    )

    state["record_id"] = _first_non_empty(record.get("record_id"), state.get("record_id"))
    state["target_record_id"] = _first_non_empty(record.get("record_id"), state.get("target_record_id"))
    state["latest_fields"] = dict(fields)
    return state


def _normalize_mapping_of_numbers(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    normalized: dict[str, float] = {}
    for key, raw_value in value.items():
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        normalized[normalized_key] = _coerce_number(raw_value)
    return normalized


def _normalize_mapping_of_attachments(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, Mapping):
        return {}
    normalized: dict[str, list[dict[str, Any]]] = {}
    for key, raw_value in value.items():
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        refs = _normalize_attachment_items(raw_value)
        if refs:
            normalized[normalized_key] = [dict(ref) for ref in refs]
    return normalized


def _normalize_attachment_items(value: Any) -> list[dict[str, Any]]:
    if value in (None, "", [], (), {}):
        return []
    if isinstance(value, Mapping):
        if any(key in value for key in ("file_token", "url", "tmp_url", "download_url", "path", "local_path")):
            return [dict(value)]
        nested_value = value.get("items") or value.get("value") or value.get("files") or value.get("attachments")
        if nested_value is not None:
            return _normalize_attachment_items(nested_value)
        return [dict(value)]
    if isinstance(value, (str, bytes, bytearray)):
        text = str(value).strip()
        if not text:
            return []
        if text.startswith(("http://", "https://")):
            return [{"url": text}]
        if Path(text).exists():
            return [{"path": text}]
        return [{"file_token": text}]
    if isinstance(value, Sequence):
        items: list[dict[str, Any]] = []
        for item in value:
            items.extend(_normalize_attachment_items(item))
        return items
    return [{"value": value}]


def _download_remote_attachment_bytes(
    download_url: str,
    *,
    session: requests.Session | None = None,
) -> bytes:
    if not download_url:
        raise ValueError("download_url is required")

    sessions: list[Any] = []
    if session is not None:
        sessions.append(session)
    sessions.append(requests)

    last_error: Exception | None = None
    for candidate in sessions:
        try:
            response = candidate.get(download_url, timeout=60)  # type: ignore[call-arg]
            if response.status_code >= 400:
                raise FeishuAPIError(
                    f"Download failed: {response.status_code}",
                    status=response.status_code,
                )
            return response.content
        except Exception as exc:  # pragma: no cover - fallback path
            last_error = exc
            continue
    if last_error is not None:
        raise FeishuAPIError(
            f"Download failed: {last_error}",
            code=getattr(last_error, "code", None),
            status=getattr(last_error, "status", None),
        ) from last_error
    raise FeishuAPIError("Download failed")


def _infer_attachment_file_name(item: Mapping[str, Any], *, download_url: str = "") -> str:
    file_name = _first_non_empty(item.get("file_name"), item.get("name"), item.get("filename"))
    if file_name:
        file_name = str(file_name).strip()
        if file_name:
            return file_name
    if download_url:
        candidate = download_url.split("?")[0].rstrip("/").split("/")[-1]
        if candidate:
            return candidate
    return f"attachment_{int(time.time() * 1000)}"


def _normalize_name_list(value: Any) -> list[str]:
    if value in (None, "", [], (), {}):
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, Mapping):
        maybe_name = _first_non_empty(value.get("name"), value.get("field_name"), value.get("text"))
        if maybe_name:
            return [maybe_name]
        nested = value.get("items") or value.get("value") or value.get("list") or value.get("options")
        if nested is not None:
            return _normalize_name_list(nested)
        return []
    if isinstance(value, Sequence):
        names: list[str] = []
        seen: set[str] = set()
        for item in value:
            for name in _normalize_name_list(item):
                if name in seen:
                    continue
                seen.add(name)
                names.append(name)
        return names
    text = str(value).strip()
    return [text] if text else []


def _extract_contact_items(raw_contact_payload: Any) -> list[Any]:
    if isinstance(raw_contact_payload, Mapping):
        data = raw_contact_payload.get("data")
        if isinstance(data, Mapping):
            items = data.get("list")
            if isinstance(items, Sequence):
                return list(items)
        items = raw_contact_payload.get("list")
        if isinstance(items, Sequence):
            return list(items)
        if "name" in raw_contact_payload or "has" in raw_contact_payload:
            return [raw_contact_payload]
    if isinstance(raw_contact_payload, Sequence) and not isinstance(raw_contact_payload, (str, bytes, bytearray)):
        return list(raw_contact_payload)
    return []


def _build_record_time_timestamp_ms(value: Any) -> int:
    timestamp_ms = _coerce_datetime_timestamp_ms(value)
    if timestamp_ms:
        return timestamp_ms
    now = datetime.now()
    local_midnight = datetime(now.year, now.month, now.day)
    return int(local_midnight.timestamp() * 1000)


def _coerce_datetime_timestamp_ms(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return 0
    if timestamp > 10_000_000_000:
        return int(timestamp)
    return int(timestamp * 1000)


def _coerce_number(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text:
        return 0.0
    multiplier = 1.0
    lower_text = text.lower()
    suffix_multipliers = (
        ("亿", 100_000_000.0),
        ("万", 10_000.0),
        ("w", 10_000.0),
        ("b", 1_000_000_000.0),
        ("m", 1_000_000.0),
        ("k", 1_000.0),
    )
    for suffix, suffix_multiplier in suffix_multipliers:
        if lower_text.endswith(suffix):
            multiplier = suffix_multiplier
            text = text[: -len(suffix)]
            break
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return 0.0
    try:
        return float(match.group(0)) * multiplier
    except (TypeError, ValueError):
        return 0.0


def _format_w_unit_display_field(value: Any) -> str:
    if value in (None, ""):
        return ""
    number = _coerce_number(value)
    if number == 0 and not _looks_like_zero(value):
        return _stringify_scalar_field(value)
    if abs(number) >= 10_000:
        return f"{_format_trimmed_decimal(number / 10_000, max_digits=2)}W"
    return _format_trimmed_decimal(number, max_digits=2)


def _format_trimmed_decimal(value: float, *, max_digits: int) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.{max_digits}f}".rstrip("0").rstrip(".")


def _looks_like_zero(value: Any) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) == 0
    text = str(value).strip().replace(",", "").replace(" ", "").lower()
    if text in {"0", "0.0", "0.00"}:
        return True
    return bool(re.fullmatch(r"[-+]?0+(?:\.0+)?(?:万|亿|w|k|m|b)?", text))


def _coerce_int(value: Any, *, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _stringify_scalar_field(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)) and float(value).is_integer():
        return str(int(value))
    return str(value).strip()


def _merge_unique_values(existing: Any, incoming: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in list(_normalize_name_list(existing)) + list(_normalize_name_list(incoming)):
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _first_non_empty(*values: Any) -> str:
    for value in values:
        if value in (None, "", [], (), {}):
            continue
        if isinstance(value, Mapping):
            for nested_key in ("value", "text", "name", "link", "channel_name"):
                nested = value.get(nested_key)
                if nested not in (None, "", [], (), {}):
                    text = str(nested).strip()
                    if text:
                        return text
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _is_truthy(value: Any) -> bool:
    if value in (None, "", [], (), {}):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _has_meaningful_value(value: Any) -> bool:
    if value in (None, "", [], (), {}):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_has_meaningful_value(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_has_meaningful_value(item) for item in value)
    return True
