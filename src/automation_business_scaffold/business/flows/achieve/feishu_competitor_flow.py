from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from automation_business_scaffold.infrastructure.feishu.api import FeishuBitableClient
from automation_business_scaffold.business.flows.fastmoss_product_flow import (
    discover_fastmoss_keyword_candidates_via_browser,
    fetch_fastmoss_product_sales_via_browser,
    validate_fastmoss_login_via_browser,
)
from automation_business_scaffold.business.flows.tiktok_product_flow import (
    TikTokSecurityCheckError,
    TikTokProductUnavailableError,
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
DEFAULT_FASTMOSS_PRICE_FIELD_NAME = "Fastmoss价格"
DEFAULT_PRODUCT_STATUS_FIELD_NAME = "商品状态"
DEFAULT_FASTMOSS_SCREENSHOT_FIELD_NAME = "Fastmoss截图"
DEFAULT_YESTERDAY_SALES_FIELD_NAME = "昨日销量"
DEFAULT_7D_SALES_FIELD_NAME = "近7天销量"
DEFAULT_90D_SALES_FIELD_NAME = "近90天销量"
FASTMOSS_DEPENDENT_FIELD_NAMES = (
    DEFAULT_FASTMOSS_PRICE_FIELD_NAME,
    DEFAULT_FASTMOSS_SCREENSHOT_FIELD_NAME,
    DEFAULT_YESTERDAY_SALES_FIELD_NAME,
    DEFAULT_7D_SALES_FIELD_NAME,
    DEFAULT_90D_SALES_FIELD_NAME,
)
UNAVAILABLE_PRODUCT_STATUS_VALUE = "已下架/区域不可售"
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
    DEFAULT_FASTMOSS_PRICE_FIELD_NAME,
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
    _log_single_row_timing(
        record_id=str(settings["record_id"]),
        phase="single_row_start",
        run_mode=str(settings["run_mode"]),
    )
    target = _build_table_target(settings["table_url"], settings["access_token"])
    raw_record = _load_feishu_record(target, settings["record_id"])
    fields = raw_record.get("fields", {})
    if not isinstance(fields, dict):
        fields = {}
    missing_fields = _missing_auto_update_field_names(fields)
    product_status = _normalize_status_field_value(fields.get(DEFAULT_PRODUCT_STATUS_FIELD_NAME))

    source_url = str(settings["source_url"] or _normalize_link_value(fields.get(DEFAULT_URL_FIELD_NAME))).strip()
    sku_id = str(settings["sku_id"] or fields.get(DEFAULT_SKU_FIELD_NAME) or "").strip()

    if product_status == UNAVAILABLE_PRODUCT_STATUS_VALUE:
        result_item = {
            "record_id": settings["record_id"],
            "source_url": source_url,
            "normalized_url": _normalize_existing_product_url(source_url),
            "product_id": sku_id,
            "status": "skipped_unavailable",
            "error": "",
            "unavailable_reason": product_status,
            "fields": {},
            "logical_fields": {},
            "fastmoss_snapshot": {},
            "missing_fields": missing_fields,
        }
        return {
            "summary": _summarize_status_counts([result_item]),
            "item": result_item,
            "items": [result_item],
        }
    if not missing_fields:
        result_item = {
            "record_id": settings["record_id"],
            "source_url": source_url,
            "normalized_url": _normalize_existing_product_url(source_url),
            "product_id": sku_id,
            "status": "skipped_completed",
            "error": "",
            "unavailable_reason": "",
            "fields": {},
            "logical_fields": {},
            "fastmoss_snapshot": {},
            "missing_fields": [],
        }
        return {
            "summary": _summarize_status_counts([result_item]),
            "item": result_item,
            "items": [result_item],
        }

    resolved_product_url = _resolve_tiktok_product_url(source_url=source_url, sku_id=sku_id)
    _log_single_row_timing(
        record_id=str(settings["record_id"]),
        phase="tiktok_fetch_start",
        product_url=resolved_product_url,
    )

    try:
        product = fetch_tiktok_product_record_via_browser(
            resolved_product_url,
            profile_ref=settings["profile_ref"],
            capture_page_screenshot=True,
            trace_id=str(settings["record_id"]),
        )
    except TikTokSecurityCheckError as exc:
        result_item = {
            "record_id": settings["record_id"],
            "source_url": source_url,
            "normalized_url": _normalize_existing_product_url(source_url),
            "product_id": sku_id,
            "status": "skipped_security_check",
            "error": str(exc),
            "unavailable_reason": "",
            "fields": {},
            "logical_fields": {},
            "fastmoss_snapshot": {},
            "missing_fields": missing_fields,
        }
        return {
            "summary": _summarize_status_counts([result_item]),
            "item": result_item,
            "items": [result_item],
        }
    except TikTokProductUnavailableError as exc:
        preview_fields = _build_unavailable_product_status_fields()
        result_item = {
            "record_id": settings["record_id"],
            "source_url": source_url,
            "normalized_url": _normalize_existing_product_url(source_url),
            "product_id": sku_id,
            "status": "preview_unavailable",
            "error": "",
            "unavailable_reason": str(exc),
            "fields": preview_fields,
            "logical_fields": {},
            "fastmoss_snapshot": {},
            "missing_fields": missing_fields,
        }
        if not settings["apply_mutations"]:
            return {
                "summary": _summarize_status_counts([result_item]),
                "item": result_item,
                "items": [result_item],
            }

        try:
            target.client.update_record(
                target.app_token,
                target.table_id,
                settings["record_id"],
                preview_fields,
            )
            result_item["status"] = "marked_unavailable"
        except Exception as exc_update:
            result_item["status"] = "update_failed"
            result_item["error"] = str(exc_update)
        return {
            "summary": _summarize_status_counts([result_item]),
            "item": result_item,
            "items": [result_item],
        }
    product = TikTokProductRecord.from_dict(
        {
            **product.to_dict(),
            "source_url": resolved_product_url,
            "normalized_url": normalize_tiktok_product_url(product.normalized_url or resolved_product_url),
        }
    )
    _validate_product_for_single_row_update(product)
    _log_single_row_timing(
        record_id=str(settings["record_id"]),
        phase="tiktok_product_ready",
        product_id=product.product_id,
        normalized_url=product.normalized_url,
        missing_fields="|".join(missing_fields),
    )

    fastmoss_snapshot: FastMossProductSalesSnapshot | None = None
    if _single_row_needs_fastmoss(missing_fields):
        _log_single_row_timing(
            record_id=str(settings["record_id"]),
            phase="fastmoss_fetch_start",
            product_id=product.product_id,
            missing_fields="|".join(missing_fields),
        )
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
            verify_login=settings["verify_fastmoss_login"],
        )
        _log_single_row_timing(
            record_id=str(settings["record_id"]),
            phase="fastmoss_fetch_ready",
            product_id=product.product_id,
        )
    preview_fields = _build_single_row_write_fields(
        product=product,
        fastmoss_snapshot=fastmoss_snapshot,
    )
    selected_preview_fields = _select_single_row_write_fields(
        preview_fields,
        existing_fields=fields,
    )

    result_item: dict[str, Any] = {
        "record_id": settings["record_id"],
        "source_url": source_url,
        "normalized_url": product.normalized_url,
        "product_id": product.product_id,
        "status": "preview" if selected_preview_fields else "skipped_completed",
        "error": "",
        "unavailable_reason": "",
        "fields": selected_preview_fields,
        "logical_fields": product.to_dict(),
        "fastmoss_snapshot": fastmoss_snapshot.to_dict() if fastmoss_snapshot is not None else {},
        "missing_fields": missing_fields,
    }

    if not settings["apply_mutations"]:
        return {
            "summary": _summarize_status_counts([result_item]),
            "item": result_item,
            "items": [result_item],
        }

    if not selected_preview_fields:
        return {
            "summary": _summarize_status_counts([result_item]),
            "item": result_item,
            "items": [result_item],
        }

    writable_fields = _prepare_writable_fields(
        client=target.client,
        app_token=target.app_token,
        preview_fields=selected_preview_fields,
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


def _log_single_row_timing(*, record_id: str, phase: str, **extra: Any) -> None:
    epoch_ms = int(time.time() * 1000)
    detail = " ".join(
        f"{key}={str(value)}"
        for key, value in extra.items()
        if str(value or "").strip()
    )
    message = (
        f"[single-row-timing] epoch_ms={epoch_ms} "
        f"record_id={record_id} phase={phase}"
    )
    if detail:
        message = f"{message} {detail}"
    print(message, flush=True)


def run_feishu_clear_row_by_url(params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_clear_row_settings(params)
    target = _build_table_target(settings["table_url"], settings["access_token"])
    record_index = _build_existing_record_index(target.client, target)
    record_id = str(record_index["by_url"].get(settings["normalized_url"], "") or "").strip()

    if not record_id:
        result_item = {
            "record_id": "",
            "source_url": settings["source_url"],
            "normalized_url": settings["normalized_url"],
            "status": "not_found",
            "error": "",
            "cleared_fields": [],
            "fields": {},
        }
        return {
            "summary": _summarize_status_counts([result_item]),
            "item": result_item,
            "items": [result_item],
        }

    raw_record = _load_feishu_record(target, record_id)
    fields = raw_record.get("fields", {})
    if not isinstance(fields, dict):
        fields = {}
    clear_fields = _build_clear_row_write_fields(fields, url_field_name=settings["url_field_name"])

    if not clear_fields:
        result_item = {
            "record_id": record_id,
            "source_url": settings["source_url"],
            "normalized_url": settings["normalized_url"],
            "status": "skipped_already_cleared",
            "error": "",
            "cleared_fields": [],
            "fields": {},
        }
        return {
            "summary": _summarize_status_counts([result_item]),
            "item": result_item,
            "items": [result_item],
        }

    result_item = {
        "record_id": record_id,
        "source_url": settings["source_url"],
        "normalized_url": settings["normalized_url"],
        "status": "preview_cleared",
        "error": "",
        "cleared_fields": sorted(clear_fields.keys()),
        "fields": clear_fields,
    }
    if not settings["apply_mutations"]:
        return {
            "summary": _summarize_status_counts([result_item]),
            "item": result_item,
            "items": [result_item],
        }

    target.client.update_record(
        target.app_token,
        target.table_id,
        record_id,
        clear_fields,
    )
    result_item["status"] = "cleared"
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
        verify_login=settings["verify_fastmoss_login"],
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


def run_fastmoss_login_check(params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_fastmoss_login_settings(params)
    payload = validate_fastmoss_login_via_browser(
        profile_ref=settings["profile_ref"],
        fastmoss_phone=settings["fastmoss_phone"],
        fastmoss_password=settings["fastmoss_password"],
        fastmoss_phone_env=settings["fastmoss_phone_env"],
        fastmoss_password_env=settings["fastmoss_password_env"],
        step_delay_sec=settings["step_delay_sec"],
        login_settle_sec=settings["login_settle_sec"],
    )
    return {
        "summary": {
            "total": 1,
            "counts": {"validated": 1},
        },
        "item": payload,
        "items": [payload],
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
    product_status = _normalize_status_field_value(fields.get(DEFAULT_PRODUCT_STATUS_FIELD_NAME))
    normalized_url = _normalize_existing_product_url(source_url)
    missing_fields = _missing_auto_update_field_names(fields)
    if product_status == UNAVAILABLE_PRODUCT_STATUS_VALUE:
        return {
            "record_id": record_id,
            "source_url": source_url,
            "normalized_url": normalized_url,
            "sku_id": sku_id,
            "status": "skipped_unavailable",
            "error": "",
            "missing_fields": missing_fields,
        }
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
        "verify_fastmoss_login": _coerce_bool(
            params.get("verify_fastmoss_login", params.get("fastmoss_verify_login")),
            default=False,
        ),
        "apply_mutations": _should_apply_mutations(run_mode),
    }


def _build_clear_row_settings(params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_feishu_table_settings(params)
    source_url = str(
        params.get("product_url")
        or params.get("source_url")
        or params.get("url")
        or ""
    ).strip()
    if not source_url:
        raise ValueError("url is required")

    return {
        **settings,
        "source_url": source_url,
        "normalized_url": normalize_tiktok_product_url(source_url),
        "apply_mutations": _should_apply_mutations(settings["run_mode"]),
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
        "verify_fastmoss_login": _coerce_bool(
            params.get("verify_fastmoss_login", params.get("fastmoss_verify_login")),
            default=True,
        ),
    }


def _build_fastmoss_login_settings(params: dict[str, Any]) -> dict[str, Any]:
    return {
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
    fastmoss_snapshot: FastMossProductSalesSnapshot | None,
) -> dict[str, Any]:
    stage1_fields = build_feishu_bitable_record(
        product,
        field_mapping=DEFAULT_SINGLE_ROW_FIELD_MAPPING,
    )["fields"]
    stage1_fields[DEFAULT_URL_FIELD_NAME] = _build_link_value(product.normalized_url)
    stage1_fields[DEFAULT_RECORD_DATE_FIELD_NAME] = _current_record_date()

    fastmoss_fields = _build_fastmoss_write_fields(fastmoss_snapshot) if fastmoss_snapshot else {}
    return {
        **stage1_fields,
        **fastmoss_fields,
    }


def _select_single_row_write_fields(
    preview_fields: dict[str, Any],
    *,
    existing_fields: dict[str, Any],
) -> dict[str, Any]:
    selected_fields: dict[str, Any] = {}
    for field_name, value in preview_fields.items():
        if field_name == DEFAULT_RECORD_DATE_FIELD_NAME:
            continue
        if not _field_has_value(existing_fields.get(field_name)):
            selected_fields[field_name] = value
    if selected_fields and DEFAULT_RECORD_DATE_FIELD_NAME in preview_fields:
        selected_fields[DEFAULT_RECORD_DATE_FIELD_NAME] = preview_fields[DEFAULT_RECORD_DATE_FIELD_NAME]
    return selected_fields


def _build_fastmoss_write_fields(snapshot: FastMossProductSalesSnapshot) -> dict[str, Any]:
    screenshot_path = Path(snapshot.detail_page_screenshot_local_path)
    if not snapshot.detail_page_screenshot_local_path or not screenshot_path.exists():
        raise ValueError("FastMoss detail screenshot is required")

    return {
        DEFAULT_FASTMOSS_PRICE_FIELD_NAME: snapshot.fastmoss_price_amount,
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


def _single_row_needs_fastmoss(missing_fields: list[str]) -> bool:
    return any(field_name in FASTMOSS_DEPENDENT_FIELD_NAMES for field_name in missing_fields)


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


def _build_clear_row_write_fields(fields: dict[str, Any], *, url_field_name: str) -> dict[str, Any]:
    clear_fields: dict[str, Any] = {}
    for field_name, value in fields.items():
        if field_name == url_field_name:
            continue
        clear_fields[field_name] = _empty_field_value(value)
    return clear_fields


def _empty_field_value(value: Any) -> Any:
    if isinstance(value, list):
        return []
    if isinstance(value, (int, float)):
        return None
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


def _normalize_status_field_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            normalized = _normalize_status_field_value(item)
            if normalized:
                return normalized
        return ""
    if isinstance(value, dict):
        for key in ("text", "name", "value"):
            normalized = _normalize_status_field_value(value.get(key))
            if normalized:
                return normalized
        return ""
    return str(value).strip()


def _build_unavailable_product_status_fields() -> dict[str, Any]:
    return {
        DEFAULT_PRODUCT_STATUS_FIELD_NAME: UNAVAILABLE_PRODUCT_STATUS_VALUE,
    }


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
