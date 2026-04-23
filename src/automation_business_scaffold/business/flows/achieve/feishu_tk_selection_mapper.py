from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from automation_business_scaffold.business.flows.tiktok_product_flow import (
    extract_tiktok_product_id,
    normalize_tiktok_product_url,
)
from automation_business_scaffold.infrastructure.feishu.api import FeishuBitableClient, parse_table_url


FEISHU_TK_SELECTION_MAPPER_CODE = "feishu_tk_selection_product_ingest_v1"
DEFAULT_FEISHU_TK_SELECTION_TABLE_URL = (
    "https://ecncxlbv3k1g.feishu.cn/base/KzJXbZWunalHHVs4OkYcvk5gnxc"
    "?table=tblpF46y6SkmVCE5&view=vewhXPD4x1"
)

FIELD_PRODUCT_LINK = "商品链接"
FIELD_MAIN_IMAGE = "商品主图"
FIELD_GALLERY_IMAGES = "商品侧边栏图片"
FIELD_PRODUCT_ID = "商品ID"
FIELD_SHOP_NAME = "店铺名称"
FIELD_TITLE = "商品标题"
FIELD_PRICE = "商品当前价格"
FIELD_COMMENT_COUNT = "商品评论数"
FIELD_DESCRIPTION = "商品描述"
FIELD_RATING = "商品评分"
FIELD_YEAR_SALES = "今年总销量"
FIELD_MARKETING_CHART = "出单种类占比图"
FIELD_TREND_CHART = "销量趋势图"
FIELD_SKU_CHART = "SKU销量占比图"
FIELD_PARENT_SPEC = "父体规格"
FIELD_PARENT_IMAGE = "父体图片"
FIELD_RECORD_DATE = "记录日期"
FIELD_BAD_REVIEW_SUMMARY = "差评整理"
FIELD_PRODUCT_STATUS = "商品状态"

PRODUCT_STATUS_UNAVAILABLE = "已下架/区域不可售"

TK_SELECTION_FIELDS = (
    FIELD_PRODUCT_LINK,
    FIELD_MAIN_IMAGE,
    FIELD_GALLERY_IMAGES,
    FIELD_PRODUCT_ID,
    FIELD_SHOP_NAME,
    FIELD_TITLE,
    FIELD_PRICE,
    FIELD_COMMENT_COUNT,
    FIELD_DESCRIPTION,
    FIELD_RATING,
    FIELD_YEAR_SALES,
    FIELD_MARKETING_CHART,
    FIELD_TREND_CHART,
    FIELD_SKU_CHART,
    FIELD_PARENT_SPEC,
    FIELD_PARENT_IMAGE,
    FIELD_RECORD_DATE,
    FIELD_BAD_REVIEW_SUMMARY,
    FIELD_PRODUCT_STATUS,
)


class FeishuTKSelectionMapper:
    """Flow-level mapper for the Feishu TK选品收集 table."""

    mapper_code = FEISHU_TK_SELECTION_MAPPER_CODE
    product_link_field = FIELD_PRODUCT_LINK
    product_id_field = FIELD_PRODUCT_ID
    all_fields = TK_SELECTION_FIELDS
    required_for_skip_decision = (
        FIELD_PRODUCT_LINK,
        FIELD_MAIN_IMAGE,
        FIELD_PRODUCT_ID,
        FIELD_SHOP_NAME,
        FIELD_TITLE,
        FIELD_PRICE,
        FIELD_COMMENT_COUNT,
        FIELD_RATING,
        FIELD_YEAR_SALES,
        FIELD_MARKETING_CHART,
        FIELD_TREND_CHART,
        FIELD_RECORD_DATE,
    )
    required_for_writeback = (
        FIELD_PRODUCT_LINK,
        FIELD_MAIN_IMAGE,
        FIELD_PRODUCT_ID,
        FIELD_SHOP_NAME,
        FIELD_TITLE,
        FIELD_PRICE,
        FIELD_COMMENT_COUNT,
        FIELD_RATING,
        FIELD_YEAR_SALES,
        FIELD_MARKETING_CHART,
        FIELD_TREND_CHART,
        FIELD_RECORD_DATE,
    )
    optional_for_writeback = (
        FIELD_GALLERY_IMAGES,
        FIELD_DESCRIPTION,
        FIELD_SKU_CHART,
        FIELD_PARENT_SPEC,
        FIELD_PARENT_IMAGE,
        FIELD_BAD_REVIEW_SUMMARY,
    )
    required_for_status_writeback = (
        FIELD_PRODUCT_STATUS,
        FIELD_RECORD_DATE,
    )

    def normalize_product_url(self, value: Any) -> str:
        raw_value = normalize_feishu_link_value(value)
        if not raw_value:
            return ""
        try:
            return normalize_tiktok_product_url(raw_value)
        except ValueError:
            return ""

    def extract_locator(self, record: Mapping[str, Any]) -> dict[str, Any]:
        fields = _record_fields(record)
        source_url = normalize_feishu_link_value(fields.get(FIELD_PRODUCT_LINK))
        normalized_url = self.normalize_product_url(source_url)
        product_id = _first_non_empty(
            fields.get(FIELD_PRODUCT_ID),
            extract_tiktok_product_id(normalized_url or source_url),
        )
        return {
            "record_id": str(record.get("record_id", "") or "").strip(),
            "source_url": source_url,
            "normalized_url": normalized_url,
            "product_id": product_id,
        }

    def record_matches_url(self, record: Mapping[str, Any], product_url: str) -> bool:
        target_url = self.normalize_product_url(product_url)
        target_product_id = extract_tiktok_product_id(target_url or str(product_url or ""))
        locator = self.extract_locator(record)
        if target_url and locator["normalized_url"] == target_url:
            return True
        return bool(target_product_id and locator["product_id"] == target_product_id)

    def missing_fields(
        self,
        fields: Mapping[str, Any],
        *,
        required_fields: tuple[str, ...] | list[str] | None = None,
    ) -> list[str]:
        required = tuple(required_fields or self.required_for_skip_decision)
        return [field_name for field_name in required if not field_has_value(fields.get(field_name))]

    def evaluate_record(
        self,
        record: Mapping[str, Any],
        *,
        product_url: str,
        table_url: str = "",
    ) -> dict[str, Any]:
        fields = _record_fields(record)
        locator = self.extract_locator(record)
        missing = self.missing_fields(fields, required_fields=self.required_for_skip_decision)
        product_status = normalize_feishu_text_value(fields.get(FIELD_PRODUCT_STATUS))
        status = "needs_ingest" if missing else "skipped_completed"
        if self.is_unavailable_status(product_status):
            status = "skipped_unavailable"
        return {
            "mapper_code": self.mapper_code,
            "status": status,
            "product_status": product_status,
            "record_id": locator["record_id"],
            "source_record_id": locator["record_id"],
            "product_url": locator["normalized_url"] or self.normalize_product_url(product_url),
            "normalized_url": locator["normalized_url"] or self.normalize_product_url(product_url),
            "product_id": locator["product_id"],
            "table_url": table_url,
            "required_for_skip_decision": list(self.required_for_skip_decision),
            "required_missing_fields": missing,
            "required_present_fields": [
                field_name
                for field_name in self.required_for_skip_decision
                if field_name not in set(missing)
            ],
            "source_record": dict(record),
            "source_fields": dict(fields),
        }

    def build_writeback_fields(
        self,
        product_result: Mapping[str, Any],
        *,
        table_read_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = _as_mapping(_as_mapping(table_read_result).get("source_fields"))
        tiktok_product = _tiktok_product_fields(product_result)
        normalized_url = _first_non_empty(
            tiktok_product.get("normalized_url"),
            _as_mapping(table_read_result).get("normalized_url"),
            _as_mapping(table_read_result).get("product_url"),
            _as_mapping(product_result).get("normalized_url"),
        )
        if normalized_url:
            try:
                normalized_url = normalize_tiktok_product_url(normalized_url)
            except ValueError:
                pass

        fields: dict[str, Any] = {}
        _set_if_present(fields, FIELD_PRODUCT_LINK, build_feishu_link_value(normalized_url))
        _set_if_present(fields, FIELD_PRODUCT_ID, _first_non_empty(tiktok_product.get("product_id"), product_result.get("product_id")))
        _set_if_present(fields, FIELD_SHOP_NAME, tiktok_product.get("shop_name"))
        _set_if_present(fields, FIELD_TITLE, tiktok_product.get("title"))
        _set_if_present(
            fields,
            FIELD_PRICE,
            _normalize_price_number(_first_non_empty(tiktok_product.get("price_amount"), tiktok_product.get("price_text"))),
        )
        _set_if_present(
            fields,
            FIELD_COMMENT_COUNT,
            _first_present(tiktok_product.get("comment_count"), tiktok_product.get("review_count")),
        )
        _set_if_present(fields, FIELD_RATING, _first_present(tiktok_product.get("rating_score")))
        _set_if_present(fields, FIELD_YEAR_SALES, _fastmoss_28_day_sales(product_result))
        _set_if_present(fields, FIELD_DESCRIPTION, _first_non_empty(tiktok_product.get("description")))

        main_image = _main_image_local_file(product_result)
        if main_image:
            fields[FIELD_MAIN_IMAGE] = main_image
        elif field_has_value(existing.get(FIELD_MAIN_IMAGE)):
            fields[FIELD_MAIN_IMAGE] = existing[FIELD_MAIN_IMAGE]

        gallery_images = _media_local_files(product_result, media_role="product_gallery_image")
        if gallery_images:
            fields[FIELD_GALLERY_IMAGES] = gallery_images

        charts = _chart_local_files(product_result)
        if charts.get("marketing_strategy"):
            fields[FIELD_MARKETING_CHART] = charts["marketing_strategy"]
        if charts.get("overview_trend"):
            fields[FIELD_TREND_CHART] = charts["overview_trend"]
        if charts.get("sku_analysis"):
            fields[FIELD_SKU_CHART] = charts["sku_analysis"]
            parent_images = _parent_image_local_files(product_result)
            if parent_images:
                fields[FIELD_PARENT_IMAGE] = parent_images
            parent_spec = _parent_spec_text(product_result)
            _set_if_present(fields, FIELD_PARENT_SPEC, parent_spec)

        fields[FIELD_RECORD_DATE] = current_record_date_timestamp_ms()
        return fields

    def validate_writeback_fields(self, fields: Mapping[str, Any]) -> list[str]:
        return self.missing_fields(fields, required_fields=self.required_for_writeback)

    def build_product_status_writeback_fields(
        self,
        product_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        del product_result
        return {
            FIELD_PRODUCT_STATUS: PRODUCT_STATUS_UNAVAILABLE,
            FIELD_RECORD_DATE: current_record_date_timestamp_ms(),
        }

    def validate_product_status_writeback_fields(self, fields: Mapping[str, Any]) -> list[str]:
        return self.missing_fields(fields, required_fields=self.required_for_status_writeback)

    def product_result_is_unavailable(self, product_result: Mapping[str, Any]) -> bool:
        item = _as_mapping(product_result.get("item"))
        status_values = (
            product_result.get("product_status"),
            item.get("product_status"),
            product_result.get("status"),
            item.get("status"),
        )
        return any(self.is_unavailable_status(value) for value in status_values)

    def is_unavailable_status(self, value: Any) -> bool:
        normalized = normalize_feishu_text_value(value)
        return normalized in {PRODUCT_STATUS_UNAVAILABLE, "product_unavailable", "skipped_unavailable"}


def read_feishu_tk_selection_table_for_product(params: dict[str, Any]) -> dict[str, Any]:
    mapper = FeishuTKSelectionMapper()
    product_url = _first_non_empty(params.get("product_url"), params.get("source_url"), params.get("url"))
    if not product_url:
        raise ValueError("product_url is required")

    table_url = resolve_feishu_tk_selection_table_url(params)
    target = _build_feishu_table_target(params, table_url=table_url)
    records = target["client"].list_all_records(
        target["app_token"],
        target["table_id"],
        page_size=int(params.get("feishu_page_size", 100) or 100),
        view_id=target["view_id"] or None,
    )
    matched_record = next((record for record in records if mapper.record_matches_url(record, product_url)), None)
    if matched_record is None:
        raise ValueError("specified product_url was not found in Feishu TK selection table")

    item = mapper.evaluate_record(matched_record, product_url=product_url, table_url=table_url)
    if _read_bool_param(params, "force_ingest", False) or _read_bool_param(
        params,
        "feishu_tk_selection_force_ingest",
        False,
    ):
        item = {
            **item,
            "status": "needs_ingest",
            "force_ingest": True,
        }
    summary_status = item["status"]
    return {
        "summary": {"total": 1, "counts": {summary_status: 1}},
        "item": item,
        "items": [item],
        "status": summary_status,
        "mapper_code": mapper.mapper_code,
        "table": {
            "table_url": table_url,
            "app_token": target["app_token"],
            "table_id": target["table_id"],
            "view_id": target["view_id"],
            "record_count": len(records),
        },
        "product_id": item.get("product_id", ""),
        "product_url": item.get("product_url", ""),
        "normalized_url": item.get("normalized_url", ""),
        "source_record_id": item.get("source_record_id", ""),
    }


def writeback_feishu_tk_selection_table(params: dict[str, Any]) -> dict[str, Any]:
    mapper = FeishuTKSelectionMapper()
    table_read_result = _as_mapping(params.get("table_read_result"))
    product_result = _as_mapping(params.get("product_ingest_result") or params.get("product_result"))
    table_read_item = _as_mapping(table_read_result.get("item"))
    record_id = _first_non_empty(
        params.get("source_record_id"),
        table_read_item.get("source_record_id"),
        table_read_item.get("record_id"),
    )
    if not record_id:
        raise ValueError("source_record_id is required for Feishu TK selection writeback")
    if not product_result:
        raise ValueError("product_ingest_result is required for Feishu TK selection writeback")

    table_url = _first_non_empty(
        params.get("tk_selection_table_url"),
        params.get("feishu_tk_selection_table_url"),
        table_read_item.get("table_url"),
        _as_mapping(table_read_result.get("table")).get("table_url"),
        DEFAULT_FEISHU_TK_SELECTION_TABLE_URL,
    )
    target = _build_feishu_table_target(params, table_url=table_url)
    status_only_writeback = mapper.product_result_is_unavailable(product_result)
    if status_only_writeback:
        preview_fields = mapper.build_product_status_writeback_fields(product_result)
        required_for_writeback = list(mapper.required_for_status_writeback)
        missing = mapper.validate_product_status_writeback_fields(preview_fields)
        writeback_status = "status_writeback_completed"
    else:
        preview_fields = mapper.build_writeback_fields(product_result, table_read_result=table_read_item)
        required_for_writeback = list(mapper.required_for_writeback)
        missing = mapper.validate_writeback_fields(preview_fields)
        writeback_status = "writeback_completed"
    if missing:
        raise ValueError(
            "Feishu TK selection writeback required fields are missing: "
            + ", ".join(missing)
        )

    writable_fields = prepare_feishu_writable_fields(
        client=target["client"],
        app_token=target["app_token"],
        table_id=target["table_id"],
        preview_fields=preview_fields,
    )
    response = target["client"].update_record(
        target["app_token"],
        target["table_id"],
        record_id,
        writable_fields,
    )
    item = {
        "status": writeback_status,
        "record_id": record_id,
        "product_id": _first_non_empty(product_result.get("product_id"), table_read_item.get("product_id")),
        "product_url": _first_non_empty(table_read_item.get("product_url"), table_read_item.get("normalized_url")),
        "updated_fields": sorted(writable_fields),
        "required_for_writeback": required_for_writeback,
        "record_date": preview_fields.get(FIELD_RECORD_DATE),
        "product_status": preview_fields.get(FIELD_PRODUCT_STATUS, ""),
        "mapper_code": mapper.mapper_code,
    }
    return {
        "summary": {"total": 1, "counts": {writeback_status: 1}},
        "item": item,
        "items": [item],
        "status": writeback_status,
        "table": {
            "table_url": table_url,
            "app_token": target["app_token"],
            "table_id": target["table_id"],
            "view_id": target["view_id"],
        },
        "preview_fields": preview_fields,
        "writable_fields": writable_fields,
        "required_for_writeback": required_for_writeback,
        "optional_for_writeback": list(mapper.optional_for_writeback),
        "feishu_response": response,
    }


def resolve_feishu_tk_selection_table_url(params: Mapping[str, Any]) -> str:
    explicit_url = _first_non_empty(
        params.get("tk_selection_table_url"),
        params.get("feishu_tk_selection_table_url"),
    )
    if explicit_url:
        return explicit_url
    if _generic_table_url_is_tk_selection(params):
        return _first_non_empty(params.get("table_url"), DEFAULT_FEISHU_TK_SELECTION_TABLE_URL)
    return DEFAULT_FEISHU_TK_SELECTION_TABLE_URL


def resolve_feishu_access_token(params: Mapping[str, Any]) -> str:
    for direct_key in ("feishu_access_token", "access_token"):
        direct_token = str(params.get(direct_key, "") or "").strip()
        if direct_token:
            return direct_token

    for env_key in ("feishu_access_token_env", "access_token_env"):
        name_or_value = str(params.get(env_key, "") or "").strip()
        if not name_or_value:
            continue
        env_value = os.getenv(name_or_value, "").strip()
        return env_value or name_or_value

    raise ValueError("feishu_access_token/access_token or feishu_access_token_env/access_token_env is required")


def prepare_feishu_writable_fields(
    *,
    client: FeishuBitableClient,
    app_token: str,
    table_id: str = "",
    preview_fields: Mapping[str, Any],
) -> dict[str, Any]:
    field_schema = _feishu_field_schema_by_name(client=client, app_token=app_token, table_id=table_id)
    writable_fields: dict[str, Any] = {}
    for column_name, value in preview_fields.items():
        if _is_local_file_payload(value):
            writable_fields[column_name] = [_upload_local_file(client=client, app_token=app_token, value=value)]
            continue
        if isinstance(value, list) and any(_is_local_file_payload(item) for item in value):
            attachments: list[dict[str, str]] = []
            for item in value:
                if _is_local_file_payload(item):
                    attachments.append(_upload_local_file(client=client, app_token=app_token, value=item))
            writable_fields[column_name] = attachments
            continue
        if column_name == FIELD_PRODUCT_LINK and isinstance(value, Mapping):
            writable_fields[column_name] = (
                dict(value)
                if _field_accepts_link_object(field_schema.get(column_name))
                else normalize_feishu_link_value(value)
            )
            continue
        writable_fields[column_name] = value
    return writable_fields


def _feishu_field_schema_by_name(
    *,
    client: FeishuBitableClient,
    app_token: str,
    table_id: str,
) -> dict[str, dict[str, Any]]:
    if not table_id:
        return {}
    try:
        fields = client.list_all_fields(app_token, table_id)
    except Exception:
        return {}
    return {
        str(field.get("field_name") or ""): dict(field)
        for field in fields
        if isinstance(field, Mapping) and str(field.get("field_name") or "")
    }


def _field_accepts_link_object(field_schema: Mapping[str, Any] | None) -> bool:
    if not isinstance(field_schema, Mapping) or not field_schema:
        return True
    ui_type = str(field_schema.get("ui_type") or "").strip().lower()
    if ui_type in {"url", "link"}:
        return True
    field_type = field_schema.get("type")
    return str(field_type) in {"15"}


def normalize_feishu_link_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("link") or value.get("url") or value.get("text") or "").strip()
    if isinstance(value, list):
        for item in value:
            normalized = normalize_feishu_link_value(item)
            if normalized:
                return normalized
        return ""
    return str(value or "").strip()


def normalize_feishu_text_value(value: Any) -> str:
    if isinstance(value, dict):
        return _first_non_empty(
            value.get("text"),
            value.get("name"),
            value.get("value"),
            value.get("option"),
            value.get("link"),
            value.get("url"),
        )
    if isinstance(value, list):
        values = [normalize_feishu_text_value(item) for item in value]
        return next((item for item in values if item), "")
    return str(value or "").strip()


def build_feishu_link_value(value: str) -> dict[str, str]:
    normalized = str(value or "").strip()
    return {"text": normalized, "link": normalized} if normalized else {}


def field_has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        if value.get("type") == "local_file":
            return bool(str(value.get("path", "") or "").strip())
        return any(field_has_value(item) for item in value.values())
    if isinstance(value, list):
        return any(field_has_value(item) for item in value)
    return True


def current_record_date_timestamp_ms() -> int:
    now = datetime.now()
    local_midnight = datetime(now.year, now.month, now.day)
    return int(local_midnight.timestamp() * 1000)


def _generic_table_url_is_tk_selection(params: Mapping[str, Any]) -> bool:
    mapper_code = str(params.get("field_mapper_code", "") or "").strip()
    if mapper_code == FEISHU_TK_SELECTION_MAPPER_CODE:
        return True
    table_kind = str(
        params.get("feishu_table_kind")
        or params.get("table_kind")
        or params.get("table_name")
        or ""
    ).strip()
    return table_kind in {"tk_selection", "TK选品收集"}


def _build_feishu_table_target(params: Mapping[str, Any], *, table_url: str) -> dict[str, Any]:
    table_meta = parse_table_url(table_url)
    access_token = resolve_feishu_access_token(params)
    client = FeishuBitableClient(
        access_token=access_token,
        timeout=int(params.get("feishu_timeout_seconds", 30) or 30),
    )
    return {
        "client": client,
        "app_token": table_meta["app_token"],
        "table_id": table_meta["table_id"],
        "view_id": table_meta.get("view_id", ""),
    }


def _upload_local_file(*, client: FeishuBitableClient, app_token: str, value: Mapping[str, Any]) -> dict[str, str]:
    local_path = Path(str(value.get("path", "") or "")).expanduser()
    if not local_path.exists():
        raise FileNotFoundError(f"Image file does not exist: {local_path}")
    file_token = client.upload_media(
        file_name=str(value.get("file_name") or local_path.name),
        file_data=local_path.read_bytes(),
        parent_node=app_token,
    )
    return {"file_token": file_token}


def _is_local_file_payload(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("type") == "local_file"


def _record_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    fields = record.get("fields", {})
    return dict(fields) if isinstance(fields, Mapping) else {}


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first_non_empty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _read_bool_param(params: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if not normalized:
        return default
    return normalized in {"1", "true", "yes", "y", "on"}


def _set_if_present(fields: dict[str, Any], field_name: str, value: Any) -> None:
    if field_has_value(value):
        fields[field_name] = value


def _tiktok_product_fields(product_result: Mapping[str, Any]) -> dict[str, Any]:
    tiktok_payload = _as_mapping(product_result.get("tiktok"))
    product = _as_mapping(tiktok_payload.get("product"))
    item = _as_mapping(tiktok_payload.get("item"))
    logical_fields = _as_mapping(item.get("logical_fields"))
    merged = {**product, **logical_fields}
    if not merged:
        return {}
    for key in ("product_id", "normalized_url", "main_image_local_path", "main_image_file_name", "main_image_mime_type"):
        if key not in merged or not field_has_value(merged.get(key)):
            fallback = _first_non_empty(tiktok_payload.get(key), item.get(key), product_result.get(key))
            if fallback:
                merged[key] = fallback
    return merged


def _main_image_local_file(product_result: Mapping[str, Any]) -> dict[str, Any]:
    product = _tiktok_product_fields(product_result)
    local_path = _first_non_empty(product.get("main_image_local_path"))
    if not local_path:
        for media in _media_assets(product_result):
            if _first_non_empty(media.get("media_role")) == "product_main_image":
                local_path = _first_non_empty(media.get("local_path"))
                if local_path:
                    return _local_file_payload(media, local_path=local_path)
        return {}
    return {
        "type": "local_file",
        "path": local_path,
        "file_name": _first_non_empty(product.get("main_image_file_name")) or Path(local_path).name,
        "mime_type": _first_non_empty(product.get("main_image_mime_type")),
        "source_url": _first_non_empty(product.get("main_image_url")),
    }


def _media_local_files(product_result: Mapping[str, Any], *, media_role: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for media in _media_assets(product_result):
        if _first_non_empty(media.get("media_role")) != media_role:
            continue
        local_path = _first_non_empty(media.get("local_path"))
        if not local_path or local_path in seen_paths:
            continue
        seen_paths.add(local_path)
        files.append(_local_file_payload(media, local_path=local_path))
    return files


def _media_assets(product_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    media_upload = _as_mapping(product_result.get("media_upload"))
    values = media_upload.get("uploaded_media_assets") or media_upload.get("items") or product_result.get("uploaded_media_assets")
    if not isinstance(values, list):
        return []
    return [dict(item) for item in values if isinstance(item, Mapping)]


def _local_file_payload(media: Mapping[str, Any], *, local_path: str) -> dict[str, Any]:
    return {
        "type": "local_file",
        "path": local_path,
        "file_name": _first_non_empty(media.get("file_name")) or Path(local_path).name,
        "mime_type": _first_non_empty(media.get("mime_type")),
        "source_url": _first_non_empty(media.get("source_url")),
    }


def _chart_local_files(product_result: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    visualizations = _as_mapping(product_result.get("visualizations"))
    files = _as_mapping(visualizations.get("files"))
    result: dict[str, dict[str, Any]] = {}
    for chart_name, path_value in files.items():
        local_path = _first_non_empty(path_value)
        if not local_path:
            continue
        result[chart_name] = {
            "type": "local_file",
            "path": local_path,
            "file_name": Path(local_path).name,
            "mime_type": "image/png",
        }
    return result


def _fastmoss_28_day_sales(product_result: Mapping[str, Any]) -> Any:
    fastmoss_payload = _as_mapping(_as_mapping(product_result.get("fastmoss")).get("fastmoss"))
    overview_payload = _as_mapping(fastmoss_payload.get("overview"))
    overview = _as_mapping(overview_payload.get("overview")) or overview_payload
    if not overview:
        return None

    d_type = _first_present(overview_payload.get("d_type"), overview.get("d_type"))
    if d_type not in (None, "") and _normalize_int_text(d_type) not in {"", "28"}:
        return None
    return _first_present(
        overview.get("sales_28d"),
        overview.get("sold_count"),
        overview.get("sales_count"),
        overview_payload.get("sales_28d"),
        overview_payload.get("sold_count"),
        overview_payload.get("sales_count"),
    )


def _normalize_int_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return str(int(float(str(value).replace(",", "").strip())))
    except (TypeError, ValueError):
        return str(value).strip()


def _normalize_price_number(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, int | float):
        return value
    normalized = str(value).strip()
    numeric_text = normalized.replace("$", "").replace(",", "").strip()
    try:
        return float(numeric_text)
    except (TypeError, ValueError):
        return value


def _parent_spec_text(product_result: Mapping[str, Any]) -> str:
    best_sku = _best_fastmoss_sku(product_result)
    matched_sku = _best_sku_entity(product_result, best_sku=best_sku)
    spec_name = _first_non_empty(
        matched_sku.get("spec_name"),
        _as_mapping(matched_sku.get("facts")).get("tiktok_spec_name"),
    )
    if spec_name:
        return spec_name
    best_name = _first_non_empty(best_sku.get("sku_name"))
    best_value = _first_non_empty(best_sku.get("sku_value"), best_sku.get("sku_name"))
    if best_name and best_value:
        return f"{best_name}: {best_value}"
    return best_value


def _parent_image_local_files(product_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    best_sku = _best_fastmoss_sku(product_result)
    best_keys = _best_sku_match_keys(best_sku)
    if not best_keys:
        return []
    candidates = [
        media
        for media in _media_assets(product_result)
        if _first_non_empty(media.get("media_role")) == "product_sku_image"
        and _first_non_empty(media.get("local_path"))
    ]
    matched = _best_parent_image_media(product_result, candidates=candidates, best_sku=best_sku, best_keys=best_keys)
    if not matched:
        return []
    local_path = _first_non_empty(matched.get("local_path"))
    if not local_path:
        return []
    return [_local_file_payload(matched, local_path=local_path)]


def _best_fastmoss_sku(product_result: Mapping[str, Any]) -> dict[str, Any]:
    fastmoss = _as_mapping(_as_mapping(product_result.get("fastmoss")).get("fastmoss"))
    sku_distribution = _as_mapping(fastmoss.get("sku_distribution"))
    best_sku = _as_mapping(sku_distribution.get("best_sku"))
    if best_sku:
        return best_sku
    skus_payload = _as_mapping(fastmoss.get("skus"))
    return _as_mapping(skus_payload.get("best_sku"))


def _best_sku_entity(product_result: Mapping[str, Any], *, best_sku: Mapping[str, Any]) -> dict[str, Any]:
    best_keys = _best_sku_match_keys(best_sku)
    for entity in _fact_entities(product_result):
        if not entity.get("sku_key"):
            continue
        if _entity_matches_sku(entity, best_keys):
            return entity
    return {}


def _fact_entities(product_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    persisted = _as_mapping(product_result.get("persisted"))
    values = persisted.get("fact_entities") or product_result.get("fact_entities")
    if not isinstance(values, list):
        return []
    return [dict(item) for item in values if isinstance(item, Mapping)]


def _best_sku_match_keys(best_sku: Mapping[str, Any]) -> set[str]:
    best_name = _first_non_empty(best_sku.get("sku_name"))
    best_value = _first_non_empty(best_sku.get("sku_value"), best_sku.get("sku_name"))
    raw_keys = {
        _first_non_empty(best_sku.get("sku_id")),
        best_name,
        best_value,
        f"{best_name}:{best_value}" if best_name and best_value else "",
        f"{best_name}: {best_value}" if best_name and best_value else "",
    }
    return {_normalize_lookup_key(key) for key in raw_keys if key}


def _best_parent_image_media(
    product_result: Mapping[str, Any],
    *,
    candidates: list[dict[str, Any]],
    best_sku: Mapping[str, Any],
    best_keys: set[str],
) -> dict[str, Any]:
    match_identity = _best_parent_image_match_identity(product_result, best_sku=best_sku, best_keys=best_keys)
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for media in candidates:
        score = _parent_image_media_match_score(
            media,
            property_ids=match_identity["property_ids"],
            image_keys=match_identity["image_keys"],
            text_keys=match_identity["text_keys"],
        )
        if score is None:
            continue
        scored.append((score, _media_display_order(media), media))
    if not scored:
        return {}
    scored.sort(key=lambda item: (item[0], item[1]))
    return scored[0][2]


def _best_parent_image_match_identity(
    product_result: Mapping[str, Any],
    *,
    best_sku: Mapping[str, Any],
    best_keys: set[str],
) -> dict[str, set[str]]:
    property_ids: set[str] = set()
    image_keys: set[str] = set()
    text_keys: set[str] = set(best_keys)
    for row in _fastmoss_sku_rows_matching_best(product_result, best_keys=best_keys):
        property_ids.update(_fastmoss_sku_property_ids(row))
        image_keys.update(_fastmoss_sku_image_keys(row))
        text_keys.update(_fastmoss_sku_text_keys(row))
    property_ids.update(_fastmoss_sku_property_ids(best_sku))
    image_keys.update(_fastmoss_sku_image_keys(best_sku))
    text_keys.update(_fastmoss_sku_text_keys(best_sku))
    return {
        "property_ids": {_normalize_lookup_key(key) for key in property_ids if key},
        "image_keys": {_normalize_image_lookup_key(key) for key in image_keys if key},
        "text_keys": {_normalize_lookup_key(key) for key in text_keys if key},
    }


def _fastmoss_sku_rows_matching_best(product_result: Mapping[str, Any], *, best_keys: set[str]) -> list[dict[str, Any]]:
    return [row for row in _fastmoss_sku_rows(product_result) if _fastmoss_sku_row_matches_best(row, best_keys)]


def _fastmoss_sku_rows(product_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    fastmoss = _as_mapping(_as_mapping(product_result.get("fastmoss")).get("fastmoss"))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in (_as_mapping(fastmoss.get("skus")), _as_mapping(fastmoss.get("sku_distribution"))):
        for key in ("sku_list", "list"):
            for row in _list_of_mappings(payload.get(key)):
                row_key = _first_non_empty(row.get("sku_id"), row.get("id"))
                if not row_key:
                    row_key = "|".join(sorted(_fastmoss_sku_text_keys(row)))
                if row_key and row_key in seen:
                    continue
                if row_key:
                    seen.add(row_key)
                rows.append(row)
    return rows


def _fastmoss_sku_row_matches_best(row: Mapping[str, Any], best_keys: set[str]) -> bool:
    return bool(best_keys & {_normalize_lookup_key(key) for key in _fastmoss_sku_text_keys(row) if key})


def _fastmoss_sku_property_ids(row: Mapping[str, Any]) -> set[str]:
    values = {
        _first_non_empty(row.get("sku_property_key")),
        _first_non_empty(row.get("sku_id"), row.get("id")),
    }
    for prop in _fastmoss_sku_props(row):
        values.update(
            {
                _first_non_empty(prop.get("prop_value_id"), prop.get("value_id")),
                _first_non_empty(prop.get("sku_property_key")),
            }
        )
    return {value for value in values if value}


def _fastmoss_sku_image_keys(row: Mapping[str, Any]) -> set[str]:
    values = set(_image_lookup_keys(_first_non_empty(row.get("image"), row.get("image_url"), row.get("source_url"))))
    for prop in _fastmoss_sku_props(row):
        values.update(_image_lookup_keys(_first_non_empty(prop.get("image"), prop.get("image_url"), prop.get("source_url"))))
    return {value for value in values if value}


def _fastmoss_sku_text_keys(row: Mapping[str, Any]) -> set[str]:
    values = {
        _first_non_empty(row.get("sku_name"), row.get("name")),
        _first_non_empty(row.get("spec_name")),
    }
    for prop in _fastmoss_sku_props(row):
        prop_name = _first_non_empty(prop.get("prop_name"), prop.get("name"))
        prop_value = _first_non_empty(prop.get("prop_value"), prop.get("value_name"), prop.get("value"))
        values.add(prop_value)
        if prop_name and prop_value:
            values.add(f"{prop_name}:{prop_value}")
            values.add(f"{prop_name}: {prop_value}")
    return {value for value in values if value}


def _fastmoss_sku_props(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    props = row.get("sku_sale_props") or row.get("props") or row.get("properties")
    return _list_of_mappings(props)


def _parent_image_media_match_score(
    media: Mapping[str, Any],
    *,
    property_ids: set[str],
    image_keys: set[str],
    text_keys: set[str],
) -> int | None:
    media_sku_keys = {_normalize_lookup_key(key) for key in _media_sku_keys(media) if key}
    if property_ids and media_sku_keys & property_ids:
        return 0
    media_image_keys = {_normalize_image_lookup_key(key) for key in _media_image_keys(media) if key}
    if image_keys and media_image_keys & image_keys:
        return 1
    if text_keys and media_sku_keys & text_keys:
        return 2
    return None


def _media_sku_keys(media: Mapping[str, Any]) -> set[str]:
    metadata = _as_mapping(media.get("metadata"))
    option_name = _first_non_empty(media.get("option_name"), metadata.get("option_name"), metadata.get("name"))
    option_value = _first_non_empty(media.get("option_value"), metadata.get("option_value"), metadata.get("value"))
    values = {
        _first_non_empty(media.get("sku_property_key")),
        _first_non_empty(metadata.get("sku_property_key")),
        _first_non_empty(media.get("value_id"), metadata.get("value_id")),
        _first_non_empty(media.get("prop_value_id"), metadata.get("prop_value_id")),
        option_value,
    }
    if option_name and option_value:
        values.add(f"{option_name}:{option_value}")
        values.add(f"{option_name}: {option_value}")
    return {value for value in values if value}


def _media_image_keys(media: Mapping[str, Any]) -> set[str]:
    metadata = _as_mapping(media.get("metadata"))
    values: set[str] = set()
    for value in (
        media.get("source_url"),
        media.get("image_url"),
        media.get("url"),
        metadata.get("source_url"),
        metadata.get("image_url"),
        metadata.get("url"),
        metadata.get("uri"),
    ):
        values.update(_image_lookup_keys(value))
    return values


def _media_display_order(media: Mapping[str, Any]) -> int:
    metadata = _as_mapping(media.get("metadata"))
    value = _first_present(media.get("display_order"), metadata.get("display_order"))
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _image_lookup_keys(value: Any) -> set[str]:
    text = _first_non_empty(value).replace("&amp;", "&")
    if not text:
        return set()
    keys = {_normalize_image_lookup_key(text), _normalize_image_lookup_key(text.split("?", 1)[0])}
    uri_match = re.search(r"(tos-[^/~?#\s]+/[A-Za-z0-9]+)", text)
    if uri_match:
        keys.add(_normalize_image_lookup_key(uri_match.group(1)))
    return {key for key in keys if key}


def _normalize_image_lookup_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _entity_matches_sku(entity: Mapping[str, Any], best_keys: set[str]) -> bool:
    entity_keys = {
        _first_non_empty(entity.get("sku_id")),
        _first_non_empty(entity.get("sku_name")),
        _first_non_empty(entity.get("spec_name")),
    }
    facts = _as_mapping(entity.get("facts"))
    entity_keys.update(
        {
            _first_non_empty(facts.get("tiktok_sku_name")),
            _first_non_empty(facts.get("tiktok_spec_name")),
        }
    )
    for prop in _list_of_mappings(facts.get("tiktok_properties")):
        prop_name = _first_non_empty(prop.get("name"), prop.get("prop_name"))
        prop_value = _first_non_empty(prop.get("value"), prop.get("prop_value"), prop.get("value_name"))
        entity_keys.add(prop_value)
        if prop_name and prop_value:
            entity_keys.add(f"{prop_name}:{prop_value}")
            entity_keys.add(f"{prop_name}: {prop_value}")
    return bool(best_keys & {_normalize_lookup_key(key) for key in entity_keys if key})


def _media_matches_sku(media: Mapping[str, Any], best_keys: set[str]) -> bool:
    metadata = _as_mapping(media.get("metadata"))
    media_keys = {
        _first_non_empty(media.get("sku_property_key")),
        _first_non_empty(metadata.get("sku_property_key")),
    }
    return bool(best_keys & {_normalize_lookup_key(key) for key in media_keys if key})


def _list_of_mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _normalize_lookup_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())
