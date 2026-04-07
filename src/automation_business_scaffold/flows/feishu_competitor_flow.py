from __future__ import annotations

from pathlib import Path
from typing import Any

from automation_business_scaffold.extend_script.feishu_api import FeishuBitableClient
from automation_business_scaffold.flows.fastmoss_product_flow import (
    discover_fastmoss_keyword_candidates_via_browser,
    fetch_fastmoss_product_sales_via_browser,
)
from automation_business_scaffold.flows.tiktok_product_flow import (
    build_feishu_bitable_record,
    fetch_tiktok_product_record_via_browser,
    normalize_tiktok_product_url,
)
from automation_business_scaffold.models import FastMossProductSalesSnapshot, TikTokProductRecord
from automation_business_scaffold.validators import validate_tiktok_product_record

from .tiktok_feishu_sync_flow import (
    DEFAULT_RECORD_DATE_FIELD_NAME,
    TableTarget,
    _build_link_value,
    _build_table_target,
    _coerce_bool,
    _coerce_float,
    _current_record_date,
    _extract_record_id,
    _normalize_link_value,
    _normalize_run_mode,
    _prepare_writable_fields,
    _retry_datetime_write_back,
    _should_apply_mutations,
)

DEFAULT_URL_FIELD_NAME = "产品链接"
DEFAULT_SKU_FIELD_NAME = "SKU-ID"
DEFAULT_REMARK_FIELD_NAME = "备注"
DEFAULT_FASTMOSS_SCREENSHOT_FIELD_NAME = "Fastmoss截图"
DEFAULT_YESTERDAY_SALES_FIELD_NAME = "昨日销量"
DEFAULT_7D_SALES_FIELD_NAME = "近7天销量"
DEFAULT_90D_SALES_FIELD_NAME = "近90天销量"
DEFAULT_SINGLE_ROW_FIELD_MAPPING = {
    "source_url": DEFAULT_URL_FIELD_NAME,
    "product_id": DEFAULT_SKU_FIELD_NAME,
    "main_image_file": "图片",
    "title": "标题",
    "holiday": "节日",
    "shop_name": "卖家",
    "product_page_screenshot_file": "前台截图",
    "price_amount": "价格",
}
AUTO_UPDATE_FIELD_NAMES = (
    DEFAULT_URL_FIELD_NAME,
    DEFAULT_SKU_FIELD_NAME,
    "图片",
    "标题",
    "节日",
    "卖家",
    "前台截图",
    "价格",
    DEFAULT_FASTMOSS_SCREENSHOT_FIELD_NAME,
    DEFAULT_YESTERDAY_SALES_FIELD_NAME,
    DEFAULT_7D_SALES_FIELD_NAME,
    DEFAULT_90D_SALES_FIELD_NAME,
    DEFAULT_RECORD_DATE_FIELD_NAME,
)


def run_feishu_pending_rows_scan(params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_feishu_table_settings(params)
    target = _build_table_target(settings["table_url"], settings["access_token"])
    records = target.client.list_all_records(
        app_token=target.app_token,
        table_id=target.table_id,
        page_size=100,
        view_id=target.view_id or None,
    )

    items: list[dict[str, Any]] = []
    target_rows: list[dict[str, Any]] = []
    for raw_record in records:
        row = _build_pending_row_item(raw_record)
        items.append(row)
        if row["status"] == "pending":
            target_rows.append(
                {
                    "record_id": row["record_id"],
                    "source_url": row["source_url"],
                    "normalized_url": row["normalized_url"],
                    "sku_id": row["sku_id"],
                    "missing_fields": row["missing_fields"],
                }
            )

    return {
        "summary": _summarize_status_counts(items),
        "items": items,
        "target_rows": target_rows,
        "settings": {
            "run_mode": settings["run_mode"],
            "url_field_name": settings["url_field_name"],
        },
    }


def run_feishu_single_row_update(params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_single_row_settings(params)
    target = _build_table_target(settings["table_url"], settings["access_token"])
    raw_record = _load_feishu_record(target, settings["record_id"])
    fields = raw_record.get("fields", {})
    if not isinstance(fields, dict):
        fields = {}

    source_url = str(settings["source_url"] or _normalize_link_value(fields.get(DEFAULT_URL_FIELD_NAME))).strip()
    sku_id = str(settings["sku_id"] or fields.get(DEFAULT_SKU_FIELD_NAME) or "").strip()
    resolved_product_url = _resolve_tiktok_product_url(source_url=source_url, sku_id=sku_id)

    product = fetch_tiktok_product_record_via_browser(
        resolved_product_url,
        profile_ref=settings["profile_ref"],
        capture_page_screenshot=True,
    )
    product = TikTokProductRecord.from_dict(
        {
            **product.to_dict(),
            "source_url": resolved_product_url,
            "normalized_url": normalize_tiktok_product_url(product.normalized_url or resolved_product_url),
        }
    )
    _validate_product_for_single_row_update(product)

    fastmoss_snapshot = fetch_fastmoss_product_sales_via_browser(
        product.product_id,
        profile_ref=settings["profile_ref"],
        fastmoss_phone=settings["fastmoss_phone"],
        fastmoss_password=settings["fastmoss_password"],
        fastmoss_phone_env=settings["fastmoss_phone_env"],
        fastmoss_password_env=settings["fastmoss_password_env"],
        step_delay_sec=settings["step_delay_sec"],
        login_settle_sec=settings["login_settle_sec"],
        capture_detail_screenshot=True,
    )
    preview_fields = _build_single_row_write_fields(
        product=product,
        fastmoss_snapshot=fastmoss_snapshot,
    )

    result_item: dict[str, Any] = {
        "record_id": settings["record_id"],
        "source_url": source_url,
        "normalized_url": product.normalized_url,
        "product_id": product.product_id,
        "status": "preview",
        "error": "",
        "fields": preview_fields,
        "logical_fields": product.to_dict(),
        "fastmoss_snapshot": fastmoss_snapshot.to_dict(),
        "missing_fields": _missing_auto_update_field_names(fields),
    }

    if not settings["apply_mutations"]:
        return {
            "summary": _summarize_status_counts([result_item]),
            "item": result_item,
            "items": [result_item],
        }

    writable_fields = _prepare_writable_fields(
        client=target.client,
        app_token=target.app_token,
        preview_fields=preview_fields,
    )
    try:
        target.client.update_record(
            target.app_token,
            target.table_id,
            settings["record_id"],
            writable_fields,
        )
        result_item["status"] = "updated"
        result_item["fields"] = writable_fields
    except Exception as exc:
        retry_fields = _retry_datetime_write_back(
            target=target,
            record_id=settings["record_id"],
            writable_fields=writable_fields,
            error=exc,
        )
        if retry_fields is not None:
            result_item["status"] = "updated"
            result_item["fields"] = retry_fields
        else:
            result_item["status"] = "update_failed"
            result_item["error"] = str(exc)
            result_item["fields"] = writable_fields

    return {
        "summary": _summarize_status_counts([result_item]),
        "item": result_item,
        "items": [result_item],
    }


def run_fastmoss_keyword_candidate_discovery(params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_keyword_settings(params)
    target = _build_table_target(settings["table_url"], settings["access_token"])
    existing_index = _build_existing_record_index(target.client, target)

    discovery = discover_fastmoss_keyword_candidates_via_browser(
        settings["search_keyword"],
        sales_7d_threshold=settings["sales_7d_threshold"],
        profile_ref=settings["profile_ref"],
        fastmoss_phone=settings["fastmoss_phone"],
        fastmoss_password=settings["fastmoss_password"],
        fastmoss_phone_env=settings["fastmoss_phone_env"],
        fastmoss_password_env=settings["fastmoss_password_env"],
        step_delay_sec=settings["step_delay_sec"],
        login_settle_sec=settings["login_settle_sec"],
    )

    items: list[dict[str, Any]] = []
    target_items: list[dict[str, Any]] = []
    for candidate in discovery.get("items", []):
        product_id = str(candidate.get("product_id", "")).strip()
        normalized_url = _normalize_existing_product_url(candidate.get("normalized_product_url"))
        existing_record_id = (
            existing_index["by_sku"].get(product_id)
            or existing_index["by_url"].get(normalized_url)
            or ""
        )
        item = {
            **candidate,
            "existing_record_id": existing_record_id,
            "status": "skipped_existing" if existing_record_id else "candidate_new",
        }
        items.append(item)
        if not existing_record_id:
            target_items.append(item)

    return {
        "summary": _summarize_status_counts(items),
        "items": items,
        "target_items": target_items,
        "settings": {
            "search_keyword": settings["search_keyword"],
            "sales_7d_threshold": settings["sales_7d_threshold"],
            "profile_ref": settings["profile_ref"],
        },
        "search_url": discovery.get("search_url", ""),
        "pages_scanned": discovery.get("pages_scanned", 0),
        "rows_scanned": discovery.get("rows_scanned", 0),
    }


def run_feishu_seed_row_insert(params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_seed_insert_settings(params)
    target = _build_table_target(settings["table_url"], settings["access_token"])
    existing_index = _build_existing_record_index(target.client, target)

    normalized_url = _resolve_tiktok_product_url(
        source_url=settings["source_url"],
        sku_id=settings["sku_id"],
    )
    normalized_url = normalize_tiktok_product_url(normalized_url)
    existing_record_id = (
        existing_index["by_sku"].get(settings["sku_id"])
        or existing_index["by_url"].get(normalized_url)
        or ""
    )

    preview_fields = {
        DEFAULT_SKU_FIELD_NAME: settings["sku_id"],
        DEFAULT_URL_FIELD_NAME: _build_link_value(normalized_url),
        DEFAULT_REMARK_FIELD_NAME: _build_keyword_remark(settings["search_keyword"]),
    }
    result_item: dict[str, Any] = {
        "record_id": existing_record_id,
        "product_id": settings["sku_id"],
        "normalized_url": normalized_url,
        "status": "skipped_existing" if existing_record_id else "preview",
        "error": "",
        "fields": preview_fields,
    }

    if existing_record_id or not settings["apply_mutations"]:
        return {
            "summary": _summarize_status_counts([result_item]),
            "item": result_item,
            "items": [result_item],
        }

    response = target.client.create_record(
        app_token=target.app_token,
        table_id=target.table_id,
        fields=preview_fields,
    )
    result_item["record_id"] = _extract_record_id(response)
    result_item["status"] = "inserted"
    return {
        "summary": _summarize_status_counts([result_item]),
        "item": result_item,
        "items": [result_item],
    }


def _build_pending_row_item(raw_record: dict[str, Any]) -> dict[str, Any]:
    record_id = str(raw_record.get("record_id", "")).strip()
    fields = raw_record.get("fields", {})
    if not isinstance(fields, dict):
        fields = {}

    source_url = _normalize_link_value(fields.get(DEFAULT_URL_FIELD_NAME))
    sku_id = str(fields.get(DEFAULT_SKU_FIELD_NAME, "") or "").strip()
    normalized_url = _normalize_existing_product_url(source_url)
    missing_fields = _missing_auto_update_field_names(fields)
    if not missing_fields:
        return {
            "record_id": record_id,
            "source_url": source_url,
            "normalized_url": normalized_url,
            "sku_id": sku_id,
            "status": "skipped_completed",
            "error": "",
            "missing_fields": [],
        }

    if not source_url and not sku_id:
        return {
            "record_id": record_id,
            "source_url": "",
            "normalized_url": "",
            "sku_id": "",
            "status": "blocked_missing_locator",
            "error": "产品链接 and SKU-ID are both empty",
            "missing_fields": missing_fields,
        }

    if source_url and not normalized_url and not sku_id:
        return {
            "record_id": record_id,
            "source_url": source_url,
            "normalized_url": "",
            "sku_id": "",
            "status": "blocked_invalid_url",
            "error": "产品链接 is invalid and SKU-ID is empty",
            "missing_fields": missing_fields,
        }

    return {
        "record_id": record_id,
        "source_url": source_url,
        "normalized_url": normalized_url,
        "sku_id": sku_id,
        "status": "pending",
        "error": "",
        "missing_fields": missing_fields,
    }


def _build_feishu_table_settings(params: dict[str, Any]) -> dict[str, Any]:
    table_url = str(params.get("table_url", "")).strip()
    if not table_url:
        raise ValueError("table_url is required")

    return {
        "table_url": table_url,
        "access_token": _resolve_access_token(params),
        "run_mode": _normalize_run_mode(params.get("run_mode")),
        "url_field_name": str(params.get("url_field_name") or DEFAULT_URL_FIELD_NAME).strip() or DEFAULT_URL_FIELD_NAME,
    }


def _build_single_row_settings(params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_feishu_table_settings(params)
    record_id = str(params.get("record_id") or "").strip()
    if not record_id:
        raise ValueError("record_id is required")

    run_mode = settings["run_mode"]
    return {
        **settings,
        "record_id": record_id,
        "source_url": str(
            params.get("product_url")
            or params.get("source_url")
            or params.get("url")
            or ""
        ).strip(),
        "sku_id": str(params.get("sku_id") or params.get("product_id") or "").strip(),
        "profile_ref": str(params.get("profile_ref") or "").strip() or None,
        "fastmoss_phone": str(params.get("fastmoss_phone") or "").strip() or None,
        "fastmoss_password": str(params.get("fastmoss_password") or "").strip() or None,
        "fastmoss_phone_env": str(params.get("fastmoss_phone_env") or "").strip() or None,
        "fastmoss_password_env": str(params.get("fastmoss_password_env") or "").strip() or None,
        "step_delay_sec": max(0.0, _coerce_float(params.get("step_delay_sec"), 2.0)),
        "login_settle_sec": max(0.0, _coerce_float(params.get("login_settle_sec"), 8.0)),
        "apply_mutations": _should_apply_mutations(run_mode),
    }


def _build_keyword_settings(params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_feishu_table_settings(params)
    search_keyword = str(params.get("search_keyword") or params.get("keyword") or "").strip()
    if not search_keyword:
        raise ValueError("search_keyword is required")

    sales_7d_threshold = float(params.get("sales_7d_threshold") or params.get("threshold") or 0)
    return {
        **settings,
        "search_keyword": search_keyword,
        "sales_7d_threshold": sales_7d_threshold,
        "profile_ref": str(params.get("profile_ref") or "").strip() or None,
        "fastmoss_phone": str(params.get("fastmoss_phone") or "").strip() or None,
        "fastmoss_password": str(params.get("fastmoss_password") or "").strip() or None,
        "fastmoss_phone_env": str(params.get("fastmoss_phone_env") or "").strip() or None,
        "fastmoss_password_env": str(params.get("fastmoss_password_env") or "").strip() or None,
        "step_delay_sec": max(0.0, _coerce_float(params.get("step_delay_sec"), 2.0)),
        "login_settle_sec": max(0.0, _coerce_float(params.get("login_settle_sec"), 8.0)),
    }


def _build_seed_insert_settings(params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_feishu_table_settings(params)
    sku_id = str(params.get("sku_id") or params.get("product_id") or "").strip()
    if not sku_id:
        raise ValueError("sku_id is required")

    search_keyword = str(params.get("search_keyword") or params.get("keyword") or "").strip()
    if not search_keyword:
        raise ValueError("search_keyword is required")

    run_mode = settings["run_mode"]
    return {
        **settings,
        "sku_id": sku_id,
        "search_keyword": search_keyword,
        "source_url": str(params.get("product_url") or params.get("source_url") or params.get("url") or "").strip(),
        "apply_mutations": _should_apply_mutations(run_mode),
    }


def _load_feishu_record(target: TableTarget, record_id: str) -> dict[str, Any]:
    payload = target.client.get_record(target.app_token, target.table_id, record_id)
    data = payload.get("data", {})
    if not isinstance(data, dict):
        raise ValueError(f"record_id={record_id} was not found")
    record = data.get("record") or data.get("item") or {}
    if not isinstance(record, dict):
        raise ValueError(f"record_id={record_id} was not found")
    return record


def _resolve_tiktok_product_url(*, source_url: str, sku_id: str) -> str:
    if str(source_url or "").strip():
        try:
            return normalize_tiktok_product_url(source_url)
        except ValueError:
            if not str(sku_id or "").strip():
                raise
    normalized_sku = str(sku_id or "").strip()
    if not normalized_sku:
        raise ValueError("Either 产品链接 or SKU-ID is required")
    return f"https://www.tiktok.com/shop/pdp/{normalized_sku}"


def _build_single_row_write_fields(
    *,
    product: TikTokProductRecord,
    fastmoss_snapshot: FastMossProductSalesSnapshot,
) -> dict[str, Any]:
    stage1_fields = build_feishu_bitable_record(
        product,
        field_mapping=DEFAULT_SINGLE_ROW_FIELD_MAPPING,
    )["fields"]
    stage1_fields[DEFAULT_URL_FIELD_NAME] = _build_link_value(product.normalized_url)
    stage1_fields[DEFAULT_RECORD_DATE_FIELD_NAME] = _current_record_date()

    fastmoss_fields = _build_fastmoss_write_fields(fastmoss_snapshot)
    return {
        **stage1_fields,
        **fastmoss_fields,
    }


def _build_fastmoss_write_fields(snapshot: FastMossProductSalesSnapshot) -> dict[str, Any]:
    screenshot_path = Path(snapshot.detail_page_screenshot_local_path)
    if not snapshot.detail_page_screenshot_local_path or not screenshot_path.exists():
        raise ValueError("FastMoss detail screenshot is required")

    return {
        DEFAULT_FASTMOSS_SCREENSHOT_FIELD_NAME: {
            "type": "local_file",
            "path": str(screenshot_path),
            "file_name": snapshot.detail_page_screenshot_file_name or screenshot_path.name,
            "mime_type": snapshot.detail_page_screenshot_mime_type or "image/png",
        },
        DEFAULT_YESTERDAY_SALES_FIELD_NAME: snapshot.yesterday_sales,
        DEFAULT_7D_SALES_FIELD_NAME: snapshot.sales_7d,
        DEFAULT_90D_SALES_FIELD_NAME: snapshot.sales_90d,
    }


def _validate_product_for_single_row_update(product: TikTokProductRecord) -> None:
    validate_tiktok_product_record(product, require_local_image=True)
    if not product.shop_name.strip():
        raise ValueError("TikTok product seller name is required")
    if not product.product_page_screenshot_local_path.strip():
        raise ValueError("TikTok product page screenshot is required")
    if not Path(product.product_page_screenshot_local_path).exists():
        raise ValueError("TikTok product page screenshot file does not exist")


def _build_existing_record_index(client: FeishuBitableClient, target: TableTarget) -> dict[str, dict[str, str]]:
    records = client.list_all_records(
        app_token=target.app_token,
        table_id=target.table_id,
        page_size=100,
        view_id=target.view_id or None,
    )
    by_sku: dict[str, str] = {}
    by_url: dict[str, str] = {}
    for raw_record in records:
        record_id = str(raw_record.get("record_id", "")).strip()
        fields = raw_record.get("fields", {})
        if not isinstance(fields, dict):
            continue

        sku_id = str(fields.get(DEFAULT_SKU_FIELD_NAME) or "").strip()
        if sku_id and sku_id not in by_sku:
            by_sku[sku_id] = record_id

        normalized_url = _normalize_existing_product_url(fields.get(DEFAULT_URL_FIELD_NAME))
        if normalized_url and normalized_url not in by_url:
            by_url[normalized_url] = record_id

    return {
        "by_sku": by_sku,
        "by_url": by_url,
    }


def _normalize_existing_product_url(value: Any) -> str:
    source_url = _normalize_link_value(value)
    if not source_url:
        return ""
    try:
        return normalize_tiktok_product_url(source_url)
    except ValueError:
        return ""


def _missing_auto_update_field_names(fields: dict[str, Any]) -> list[str]:
    return [field_name for field_name in AUTO_UPDATE_FIELD_NAMES if not _field_has_value(fields.get(field_name))]


def _field_has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, list):
        return len(value) > 0
    return True


def _build_keyword_remark(search_keyword: str) -> str:
    return f"通过搜索关键字：{search_keyword}"


def _summarize_status_counts(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status", "")).strip() or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return {
        "total": len(items),
        "counts": counts,
    }


def _resolve_access_token(params: dict[str, Any]) -> str:
    direct_token = str(params.get("access_token", "")).strip()
    if direct_token:
        return direct_token

    name_or_value = str(params.get("access_token_env", "")).strip()
    if not name_or_value:
        raise ValueError("access_token or access_token_env is required")

    from os import getenv

    env_value = getenv(name_or_value, "").strip()
    if env_value:
        return env_value
    return name_or_value
