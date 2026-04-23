from __future__ import annotations

import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from automation_business_scaffold.infrastructure.feishu.api import (
    FeishuBitableClient,
    parse_table_url,
)
from automation_business_scaffold.business.flows.tiktok_product_flow import (
    DEFAULT_FEISHU_FIELD_MAPPING,
    build_feishu_bitable_record,
    fetch_tiktok_product_record,
    normalize_tiktok_product_url,
)
from automation_business_scaffold.validators import (
    validate_tiktok_product_record,
    validate_tiktok_product_url,
)

DEFAULT_STEP_DELAY_SEC = 1.0
DEFAULT_STEP_DELAY_JITTER_SEC = 1.0
DEFAULT_URL_FIELD_NAME = "产品链接"
DEFAULT_RECORD_DATE_FIELD_NAME = "记录日期"
RUN_MODES_WITH_MUTATIONS = {"canary", "full_auto"}


class ExistingRecordIndex:
    def __init__(self, by_url: dict[str, str], by_sku: dict[str, str]) -> None:
        self.by_url = by_url
        self.by_sku = by_sku


class TableTarget:
    def __init__(self, client: FeishuBitableClient, app_token: str, table_id: str, view_id: str) -> None:
        self.client = client
        self.app_token = app_token
        self.table_id = table_id
        self.view_id = view_id


def run_tiktok_feishu_single_sync(params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_single_settings(params)
    target = _build_table_target(settings["table_url"], settings["access_token"])
    field_mapping = _effective_single_field_mapping(settings["field_mapping"])
    existing_index = _load_existing_record_index(
        client=target.client,
        app_token=target.app_token,
        table_id=target.table_id,
        view_id=target.view_id,
        field_mapping=field_mapping,
    )
    return sync_single_tiktok_product_url(
        product_url=settings["product_url"],
        target=target,
        field_mapping=field_mapping,
        existing_index=existing_index,
        write_back=settings["write_back"],
        step_delay_sec=settings["step_delay_sec"],
        step_delay_jitter_sec=settings["step_delay_jitter_sec"],
    )


def run_tiktok_product_link_cleanup(params: dict[str, Any]) -> dict[str, Any]:
    records_payload = load_cleanup_records(params)
    normalized_payload = normalize_cleanup_records(records_payload["records"], params)
    deletion_payload = delete_cleanup_duplicates(normalized_payload["items"], params)
    write_payload = write_back_cleanup_records(
        normalized_payload["items"],
        deletion_payload["deletion_results"],
        params,
    )
    return build_cleanup_summary(
        normalized_payload["items"],
        deletion_payload["deletion_results"],
        write_payload["update_results"],
        params,
    )


def load_cleanup_records(params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_cleanup_settings(params)
    target = _build_table_target(settings["table_url"], settings["access_token"])
    records = target.client.list_all_records(
        app_token=target.app_token,
        table_id=target.table_id,
        page_size=100,
        view_id=target.view_id or None,
    )
    return {"records": records}


def normalize_cleanup_records(records: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_cleanup_settings(params)
    items: list[dict[str, Any]] = []
    keepers_by_normalized_url: dict[str, dict[str, Any]] = {}

    for raw_row in records:
        record_id = str(raw_row.get("record_id", "")).strip()
        fields = raw_row.get("fields", {})
        if not isinstance(fields, dict):
            fields = {}

        if _cleanup_raw_row_is_empty(fields):
            items.append(
                {
                    "record_id": record_id,
                    "source_url": "",
                    "normalized_url": "",
                    "status": "delete_empty",
                    "error": "",
                    "deleted_record_ids": [],
                    "update_fields": {},
                }
            )
            continue

        source_url = _normalize_link_value(fields.get(settings["url_field_name"]))

        if not source_url:
            items.append(
                {
                    "record_id": record_id,
                    "source_url": "",
                    "normalized_url": "",
                    "status": "skipped_empty",
                    "error": "",
                    "deleted_record_ids": [],
                    "update_fields": {},
                }
            )
            continue

        try:
            normalized_url = normalize_tiktok_product_url(source_url)
        except ValueError as exc:
            items.append(
                {
                    "record_id": record_id,
                    "source_url": source_url,
                    "normalized_url": "",
                    "status": "invalid_url",
                    "error": str(exc),
                    "deleted_record_ids": [],
                    "update_fields": {},
                }
            )
            continue

        existing = keepers_by_normalized_url.get(normalized_url)
        if existing is None:
            item = {
                "record_id": record_id,
                "source_url": source_url,
                "normalized_url": normalized_url,
                "status": "keep",
                "error": "",
                "deleted_record_ids": [],
                "update_fields": _build_cleanup_update_fields(
                    normalized_url=normalized_url,
                    url_field_name=settings["url_field_name"],
                ),
            }
            keepers_by_normalized_url[normalized_url] = item
            items.append(item)
            continue

        existing["deleted_record_ids"].append(record_id)
        items.append(
            {
                "record_id": record_id,
                "source_url": source_url,
                "normalized_url": normalized_url,
                "status": "delete_duplicate",
                "error": "",
                "deleted_record_ids": [],
                "keeper_record_id": existing["record_id"],
                "update_fields": {},
            }
        )

    return {"items": items}


def delete_cleanup_duplicates(items: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_cleanup_settings(params)
    if not settings["apply_mutations"]:
        return {
            "deletion_results": [
                {
                    "record_id": item["record_id"],
                    "status": "delete_preview" if _should_delete_cleanup_row(item["status"]) else item["status"],
                    "error": item.get("error", ""),
                }
                for item in items
            ]
        }

    target = _build_table_target(settings["table_url"], settings["access_token"])
    deletion_results: list[dict[str, Any]] = []
    for item in items:
        if not _should_delete_cleanup_row(item["status"]):
            deletion_results.append(
                {
                    "record_id": item["record_id"],
                    "status": item["status"],
                    "error": item.get("error", ""),
                }
            )
            continue
        try:
            target.client.delete_record(target.app_token, target.table_id, item["record_id"])
            deletion_results.append(
                {
                    "record_id": item["record_id"],
                    "status": "deleted",
                    "error": "",
                }
            )
        except Exception as exc:
            deletion_results.append(
                {
                    "record_id": item["record_id"],
                    "status": "delete_failed",
                    "error": str(exc),
                }
            )
    return {"deletion_results": deletion_results}


def write_back_cleanup_records(
    items: list[dict[str, Any]],
    deletion_results: list[dict[str, Any]],
    params: dict[str, Any],
) -> dict[str, Any]:
    settings = _build_cleanup_settings(params)
    deletion_by_id = {item["record_id"]: item for item in deletion_results}
    update_results: list[dict[str, Any]] = []

    if not settings["apply_mutations"]:
        for item in items:
            if item["status"] != "keep" or not item["update_fields"]:
                continue
            update_results.append(
                {
                    "record_id": item["record_id"],
                    "status": "preview",
                    "error": item.get("error", ""),
                    "fields": item["update_fields"],
                }
            )
        return {"update_results": update_results}

    target = _build_table_target(settings["table_url"], settings["access_token"])
    for item in items:
        if item["status"] != "keep" or not item["update_fields"]:
            continue

        duplicate_delete_failed = any(
            deletion_by_id.get(record_id, {}).get("status") == "delete_failed"
            for record_id in item["deleted_record_ids"]
        )
        if duplicate_delete_failed:
            update_results.append(
                {
                    "record_id": item["record_id"],
                    "status": "skipped_due_to_delete_failure",
                    "error": "duplicate deletion failed",
                    "fields": item["update_fields"],
                }
            )
            continue

        try:
            target.client.update_record(target.app_token, target.table_id, item["record_id"], item["update_fields"])
            update_results.append(
                {
                    "record_id": item["record_id"],
                    "status": "updated",
                    "error": "",
                    "fields": item["update_fields"],
                }
            )
        except Exception as exc:
            update_results.append(
                {
                    "record_id": item["record_id"],
                    "status": "update_failed",
                    "error": str(exc),
                    "fields": item["update_fields"],
                }
            )

    return {"update_results": update_results}


def build_cleanup_summary(
    items: list[dict[str, Any]],
    deletion_results: list[dict[str, Any]],
    update_results: list[dict[str, Any]],
    params: dict[str, Any],
) -> dict[str, Any]:
    settings = _build_cleanup_settings(params)
    deletion_by_id = {item["record_id"]: item for item in deletion_results}
    update_by_id = {item["record_id"]: item for item in update_results}
    final_items: list[dict[str, Any]] = []

    for item in items:
        record_id = item["record_id"]
        if _should_delete_cleanup_row(item["status"]):
            deletion = deletion_by_id.get(record_id, {})
            final_items.append(
                {
                    "record_id": record_id,
                    "source_url": item["source_url"],
                    "normalized_url": item["normalized_url"],
                    "status": deletion.get("status", "delete_preview"),
                    "error": deletion.get("error", ""),
                    "deleted_record_ids": [],
                }
            )
            continue

        if item["status"] in {"invalid_url", "skipped_empty"}:
            final_items.append(
                {
                    "record_id": record_id,
                    "source_url": item["source_url"],
                    "normalized_url": item["normalized_url"],
                    "status": item["status"],
                    "error": item.get("error", ""),
                    "deleted_record_ids": item["deleted_record_ids"],
                }
            )
            continue

        update = update_by_id.get(record_id, {})
        final_items.append(
            {
                "record_id": record_id,
                "source_url": item["source_url"],
                "normalized_url": item["normalized_url"],
                "status": update.get("status", item["status"]),
                "error": update.get("error", item.get("error", "")),
                "deleted_record_ids": item["deleted_record_ids"],
            }
        )

    return {
        "summary": _summarize_status_counts(final_items),
        "items": final_items,
        "settings": {
            "run_mode": settings["run_mode"],
            "apply_mutations": settings["apply_mutations"],
            "url_field_name": settings["url_field_name"],
        },
    }


def sync_single_tiktok_product_url(
    *,
    product_url: str,
    target: TableTarget,
    field_mapping: dict[str, str],
    existing_index: ExistingRecordIndex,
    write_back: bool,
    step_delay_sec: float,
    step_delay_jitter_sec: float,
) -> dict[str, Any]:
    normalized_url = normalize_tiktok_product_url(product_url)
    validate_tiktok_product_url(normalized_url)

    existing_record_id = existing_index.by_url.get(normalized_url, "")
    if existing_record_id:
        return {
            "status": "skipped_existing",
            "record_id": existing_record_id,
            "product_url": normalized_url,
            "product_id": "",
            "fields": {},
            "duplicate_reason": "url",
            "existing_record_id": existing_record_id,
        }

    product = fetch_tiktok_product_record(normalized_url)
    validate_tiktok_product_record(product)

    existing_record_id = existing_index.by_sku.get(product.product_id, "")
    if existing_record_id:
        return {
            "status": "skipped_existing",
            "record_id": existing_record_id,
            "product_url": normalized_url,
            "product_id": product.product_id,
            "fields": {},
            "duplicate_reason": "sku",
            "existing_record_id": existing_record_id,
        }

    preview_fields = build_feishu_bitable_record(product, field_mapping=field_mapping)["fields"]

    if not write_back:
        return {
            "status": "preview",
            "record_id": "",
            "product_url": normalized_url,
            "product_id": product.product_id,
            "fields": preview_fields,
        }

    _sleep_with_jitter(step_delay_sec, step_delay_jitter_sec)
    writable_fields = _prepare_writable_fields(
        client=target.client,
        app_token=target.app_token,
        preview_fields=preview_fields,
    )

    _sleep_with_jitter(step_delay_sec, step_delay_jitter_sec)
    response = target.client.create_record(
        app_token=target.app_token,
        table_id=target.table_id,
        fields=writable_fields,
    )
    record_id = _extract_record_id(response)
    index_record_id = record_id or f"pending:{product.product_id}"
    existing_index.by_url[normalized_url] = index_record_id
    existing_index.by_sku[product.product_id] = index_record_id

    return {
        "status": "inserted",
        "record_id": record_id,
        "product_url": normalized_url,
        "product_id": product.product_id,
        "fields": writable_fields,
    }


def _build_single_settings(params: dict[str, Any]) -> dict[str, Any]:
    product_url = str(params.get("product_url") or params.get("url") or "").strip()
    if not product_url:
        raise ValueError("product_url is required")

    table_url = str(params.get("table_url", "")).strip()
    if not table_url:
        raise ValueError("table_url is required")

    run_mode = _normalize_run_mode(params.get("run_mode"))
    write_back = _coerce_bool(params.get("write_back"), default=_should_apply_mutations(run_mode))

    return {
        "product_url": product_url,
        "table_url": table_url,
        "access_token": _resolve_access_token(params),
        "run_mode": run_mode,
        "write_back": write_back,
        "step_delay_sec": max(0.0, _coerce_float(params.get("step_delay_sec"), DEFAULT_STEP_DELAY_SEC)),
        "step_delay_jitter_sec": max(
            0.0,
            _coerce_float(params.get("step_delay_jitter_sec"), DEFAULT_STEP_DELAY_JITTER_SEC),
        ),
        "field_mapping": _parse_field_mapping(params.get("field_mapping")),
    }


def _build_cleanup_settings(params: dict[str, Any]) -> dict[str, Any]:
    table_url = str(params.get("table_url", "")).strip()
    if not table_url:
        raise ValueError("table_url is required")

    url_field_name = str(params.get("url_field_name", DEFAULT_URL_FIELD_NAME)).strip()
    if not url_field_name:
        raise ValueError("url_field_name is required")

    run_mode = _normalize_run_mode(params.get("run_mode"))
    return {
        "table_url": table_url,
        "access_token": _resolve_access_token(params),
        "run_mode": run_mode,
        "apply_mutations": _should_apply_mutations(run_mode),
        "url_field_name": url_field_name,
    }


def _build_table_target(table_url: str, access_token: str) -> TableTarget:
    table_meta = parse_table_url(table_url)
    return TableTarget(
        client=FeishuBitableClient(access_token),
        app_token=table_meta["app_token"],
        table_id=table_meta["table_id"],
        view_id=table_meta.get("view_id", ""),
    )


def _load_existing_record_index(
    *,
    client: FeishuBitableClient,
    app_token: str,
    table_id: str,
    view_id: str,
    field_mapping: dict[str, str],
) -> ExistingRecordIndex:
    url_field_name = field_mapping["source_url"]
    sku_field_name = field_mapping["product_id"]
    items = client.list_all_records(
        app_token=app_token,
        table_id=table_id,
        page_size=100,
        view_id=view_id or None,
    )

    by_url: dict[str, str] = {}
    by_sku: dict[str, str] = {}
    for item in items:
        record_id = str(item.get("record_id", "")).strip()
        fields = item.get("fields", {})
        if not isinstance(fields, dict):
            continue

        existing_url = _normalize_link_value(fields.get(url_field_name))
        if existing_url:
            try:
                existing_url = normalize_tiktok_product_url(existing_url)
            except ValueError:
                existing_url = ""
        if existing_url and existing_url not in by_url:
            by_url[existing_url] = record_id

        existing_sku = str(fields.get(sku_field_name, "") or "").strip()
        if existing_sku and existing_sku not in by_sku:
            by_sku[existing_sku] = record_id

    return ExistingRecordIndex(by_url=by_url, by_sku=by_sku)


def _prepare_writable_fields(
    *,
    client: FeishuBitableClient,
    app_token: str,
    preview_fields: dict[str, Any],
) -> dict[str, Any]:
    writable_fields: dict[str, Any] = {}
    for column_name, value in preview_fields.items():
        if isinstance(value, dict) and value.get("type") == "local_file":
            local_path = Path(str(value.get("path", "")))
            if not local_path.exists():
                raise FileNotFoundError(f"Image file does not exist: {local_path}")
            file_token = client.upload_media(
                file_name=str(value.get("file_name", local_path.name)),
                file_data=local_path.read_bytes(),
                parent_node=app_token,
            )
            writable_fields[column_name] = [{"file_token": file_token}]
            continue
        writable_fields[column_name] = value
    return writable_fields


def _build_cleanup_update_fields(
    *,
    normalized_url: str,
    url_field_name: str,
) -> dict[str, Any]:
    return {
        url_field_name: _build_link_value(normalized_url),
    }


def _summarize_status_counts(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status", "")).strip() or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return {
        "total": len(items),
        "counts": counts,
    }


def _extract_record_id(response: dict[str, Any]) -> str:
    data = response.get("data", {})
    if not isinstance(data, dict):
        return ""

    direct_record_id = str(data.get("record_id", "") or "").strip()
    if direct_record_id:
        return direct_record_id

    for key in ("record", "item"):
        nested = data.get(key, {})
        if not isinstance(nested, dict):
            continue
        nested_record_id = str(nested.get("record_id", "") or "").strip()
        if nested_record_id:
            return nested_record_id

    return ""


def _effective_single_field_mapping(field_mapping: dict[str, str] | None) -> dict[str, str]:
    return DEFAULT_FEISHU_FIELD_MAPPING | (field_mapping or {})


def _parse_field_mapping(raw_mapping: Any) -> dict[str, str] | None:
    if raw_mapping is None:
        return None
    if not isinstance(raw_mapping, dict):
        raise ValueError("field_mapping must be an object when provided")
    return {str(key): str(value) for key, value in raw_mapping.items()}


def _resolve_access_token(params: dict[str, Any]) -> str:
    direct_token = str(params.get("access_token", "")).strip()
    if direct_token:
        return direct_token

    name_or_value = str(params.get("access_token_env", "")).strip()
    if not name_or_value:
        raise ValueError("access_token or access_token_env is required")

    env_value = os.getenv(name_or_value, "").strip()
    if env_value:
        return env_value

    return name_or_value


def _normalize_link_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("link") or value.get("text") or "").strip()
    return str(value or "").strip()


def _build_link_value(value: str) -> dict[str, str]:
    return {"text": value, "link": value}


def _cleanup_raw_row_is_empty(fields: dict[str, Any]) -> bool:
    return not any(_cleanup_field_has_value(value) for value in fields.values())


def _cleanup_field_has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_cleanup_field_has_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_cleanup_field_has_value(item) for item in value)
    return True


def _should_delete_cleanup_row(status: str) -> bool:
    return status in {"delete_duplicate", "delete_empty"}


def _sleep_with_jitter(delay_sec: float, jitter_sec: float) -> None:
    if delay_sec <= 0 and jitter_sec <= 0:
        return
    time.sleep(delay_sec + random.uniform(0.0, jitter_sec))


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Cannot parse boolean value from: {value}")


def _coerce_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _normalize_run_mode(value: Any) -> str:
    normalized = str(value or "draft").strip().lower() or "draft"
    if normalized == "live":
        return "canary"
    return normalized


def _should_apply_mutations(run_mode: str) -> bool:
    return run_mode in RUN_MODES_WITH_MUTATIONS


def _current_record_date() -> str:
    return datetime.now().strftime("%Y/%m/%d")


def _retry_datetime_write_back(
    *,
    target: TableTarget,
    record_id: str,
    writable_fields: dict[str, Any],
    error: Exception,
) -> dict[str, Any] | None:
    if not _is_datetime_field_conversion_error(error):
        return None
    if DEFAULT_RECORD_DATE_FIELD_NAME not in writable_fields:
        return None

    retry_fields = dict(writable_fields)
    retry_fields[DEFAULT_RECORD_DATE_FIELD_NAME] = _current_record_date_timestamp_ms()
    target.client.update_record(
        target.app_token,
        target.table_id,
        record_id,
        retry_fields,
    )
    return retry_fields


def _is_datetime_field_conversion_error(error: Exception) -> bool:
    return "DatetimeFieldConvFail" in str(error)


def _current_record_date_timestamp_ms() -> int:
    now = datetime.now()
    local_midnight = datetime(now.year, now.month, now.day)
    return int(local_midnight.timestamp() * 1000)
