from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from automation_business_scaffold.extend_script.feishu_api import (
    FeishuBitableClient,
    parse_table_url,
)
from automation_business_scaffold.flows.tiktok_product_flow import (
    DEFAULT_FEISHU_FIELD_MAPPING,
    build_feishu_bitable_record,
    download_tiktok_product_main_image,
    fetch_tiktok_product_record,
)
from automation_business_scaffold.validators import (
    validate_tiktok_product_record,
    validate_tiktok_product_url,
)

DEFAULT_STEP_DELAY_SEC = 1.0
DEFAULT_STEP_DELAY_JITTER_SEC = 1.0
DEFAULT_RECORD_DELAY_SEC = 2.0
DEFAULT_RECORD_DELAY_JITTER_SEC = 2.0
DEFAULT_PAUSE_EVERY = 5
DEFAULT_PAUSE_SEC = 8.0
DEFAULT_CONTINUE_ON_ERROR = True


@dataclass(slots=True)
class SingleSyncSettings:
    product_url: str
    table_url: str
    access_token: str
    run_mode: str
    write_back: bool
    step_delay_sec: float
    step_delay_jitter_sec: float
    field_mapping: dict[str, str] | None


@dataclass(slots=True)
class BatchSyncSettings:
    product_urls: list[str]
    table_url: str
    access_token: str
    run_mode: str
    write_back: bool
    step_delay_sec: float
    step_delay_jitter_sec: float
    record_delay_sec: float
    record_delay_jitter_sec: float
    pause_every: int
    pause_sec: float
    continue_on_error: bool
    field_mapping: dict[str, str] | None


@dataclass(slots=True)
class ExistingRecordIndex:
    by_url: dict[str, str]
    by_sku: dict[str, str]


@dataclass(slots=True)
class TableTarget:
    client: FeishuBitableClient
    app_token: str
    table_id: str


def run_tiktok_feishu_single_sync(params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_single_settings(params)
    target = _build_table_target(settings.table_url, settings.access_token)
    field_mapping = _effective_field_mapping(settings.field_mapping)
    existing_index = _load_existing_record_index(
        client=target.client,
        app_token=target.app_token,
        table_id=target.table_id,
        field_mapping=field_mapping,
    )
    return sync_single_tiktok_product_url(
        product_url=settings.product_url,
        target=target,
        field_mapping=field_mapping,
        existing_index=existing_index,
        write_back=settings.write_back,
        step_delay_sec=settings.step_delay_sec,
        step_delay_jitter_sec=settings.step_delay_jitter_sec,
    )


def run_tiktok_feishu_batch_sync(params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_batch_settings(params)
    target = _build_table_target(settings.table_url, settings.access_token)
    field_mapping = _effective_field_mapping(settings.field_mapping)
    existing_index = _load_existing_record_index(
        client=target.client,
        app_token=target.app_token,
        table_id=target.table_id,
        field_mapping=field_mapping,
    )

    items: list[dict[str, Any]] = []
    inserted = 0
    skipped_existing = 0
    previewed = 0
    failed = 0

    for index, product_url in enumerate(settings.product_urls, start=1):
        normalized_url = str(product_url).strip()
        try:
            item = sync_single_tiktok_product_url(
                product_url=normalized_url,
                target=target,
                field_mapping=field_mapping,
                existing_index=existing_index,
                write_back=settings.write_back,
                step_delay_sec=settings.step_delay_sec,
                step_delay_jitter_sec=settings.step_delay_jitter_sec,
            )
            items.append(item)
            if item["status"] == "inserted":
                inserted += 1
            elif item["status"] == "skipped_existing":
                skipped_existing += 1
            else:
                previewed += 1
        except Exception as exc:
            failed += 1
            items.append(
                {
                    "status": "failed",
                    "record_id": "",
                    "product_url": normalized_url,
                    "product_id": "",
                    "fields": {},
                    "error": str(exc),
                }
            )
            if not settings.continue_on_error:
                raise

        if index < len(settings.product_urls):
            _sleep_with_jitter(settings.record_delay_sec, settings.record_delay_jitter_sec)
            if settings.pause_every > 0 and index % settings.pause_every == 0:
                time.sleep(settings.pause_sec)

    return {
        "summary": {
            "total": len(settings.product_urls),
            "processed": len(items),
            "inserted": inserted,
            "skipped_existing": skipped_existing,
            "previewed": previewed,
            "failed": failed,
        },
        "items": items,
        "settings": {
            "run_mode": settings.run_mode,
            "write_back": settings.write_back,
            "step_delay_sec": settings.step_delay_sec,
            "step_delay_jitter_sec": settings.step_delay_jitter_sec,
            "record_delay_sec": settings.record_delay_sec,
            "record_delay_jitter_sec": settings.record_delay_jitter_sec,
            "pause_every": settings.pause_every,
            "pause_sec": settings.pause_sec,
            "continue_on_error": settings.continue_on_error,
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
    normalized_url = str(product_url).strip()
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

    _sleep_with_jitter(step_delay_sec, step_delay_jitter_sec)
    product_with_image = download_tiktok_product_main_image(product)
    validate_tiktok_product_record(product_with_image, require_local_image=True)

    feishu_record = build_feishu_bitable_record(product_with_image, field_mapping=field_mapping)
    preview_fields = feishu_record["fields"]

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


def _build_single_settings(params: dict[str, Any]) -> SingleSyncSettings:
    product_url = str(params.get("product_url") or params.get("url") or "").strip()
    if not product_url:
        raise ValueError("product_url is required")

    table_url = str(params.get("table_url", "")).strip()
    if not table_url:
        raise ValueError("table_url is required")

    run_mode = str(params.get("run_mode", "draft")).strip() or "draft"
    write_back = _coerce_bool(params.get("write_back"), default=(run_mode != "draft"))

    return SingleSyncSettings(
        product_url=product_url,
        table_url=table_url,
        access_token=_resolve_access_token(params),
        run_mode=run_mode,
        write_back=write_back,
        step_delay_sec=max(
            0.0,
            _coerce_float(params.get("step_delay_sec"), DEFAULT_STEP_DELAY_SEC),
        ),
        step_delay_jitter_sec=max(
            0.0,
            _coerce_float(params.get("step_delay_jitter_sec"), DEFAULT_STEP_DELAY_JITTER_SEC),
        ),
        field_mapping=_parse_field_mapping(params.get("field_mapping")),
    )


def _build_batch_settings(params: dict[str, Any]) -> BatchSyncSettings:
    raw_urls = params.get("product_urls")
    if not isinstance(raw_urls, (list, tuple)):
        raise ValueError("product_urls must be an array")

    product_urls = [str(item).strip() for item in raw_urls if str(item).strip()]
    if not product_urls:
        raise ValueError("product_urls must contain at least one URL")

    table_url = str(params.get("table_url", "")).strip()
    if not table_url:
        raise ValueError("table_url is required")

    run_mode = str(params.get("run_mode", "draft")).strip() or "draft"
    write_back = _coerce_bool(params.get("write_back"), default=(run_mode != "draft"))

    return BatchSyncSettings(
        product_urls=product_urls,
        table_url=table_url,
        access_token=_resolve_access_token(params),
        run_mode=run_mode,
        write_back=write_back,
        step_delay_sec=max(
            0.0,
            _coerce_float(params.get("step_delay_sec"), DEFAULT_STEP_DELAY_SEC),
        ),
        step_delay_jitter_sec=max(
            0.0,
            _coerce_float(params.get("step_delay_jitter_sec"), DEFAULT_STEP_DELAY_JITTER_SEC),
        ),
        record_delay_sec=max(
            0.0,
            _coerce_float(params.get("record_delay_sec"), DEFAULT_RECORD_DELAY_SEC),
        ),
        record_delay_jitter_sec=max(
            0.0,
            _coerce_float(
                params.get("record_delay_jitter_sec"),
                DEFAULT_RECORD_DELAY_JITTER_SEC,
            ),
        ),
        pause_every=max(0, _coerce_int(params.get("pause_every"), DEFAULT_PAUSE_EVERY)),
        pause_sec=max(0.0, _coerce_float(params.get("pause_sec"), DEFAULT_PAUSE_SEC)),
        continue_on_error=_coerce_bool(
            params.get("continue_on_error"),
            default=DEFAULT_CONTINUE_ON_ERROR,
        ),
        field_mapping=_parse_field_mapping(params.get("field_mapping")),
    )


def _build_table_target(table_url: str, access_token: str) -> TableTarget:
    table_meta = parse_table_url(table_url)
    return TableTarget(
        client=FeishuBitableClient(access_token),
        app_token=table_meta["app_token"],
        table_id=table_meta["table_id"],
    )


def _load_existing_record_index(
    *,
    client: FeishuBitableClient,
    app_token: str,
    table_id: str,
    field_mapping: dict[str, str],
) -> ExistingRecordIndex:
    url_field_name = field_mapping["source_url"]
    sku_field_name = field_mapping["product_id"]
    items = client.list_all_records(app_token=app_token, table_id=table_id, page_size=100)

    by_url: dict[str, str] = {}
    by_sku: dict[str, str] = {}
    for item in items:
        record_id = str(item.get("record_id", "")).strip()
        fields = item.get("fields", {})
        if not isinstance(fields, dict):
            continue

        existing_url = _normalize_link_value(fields.get(url_field_name))
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


def _parse_field_mapping(raw_mapping: Any) -> dict[str, str] | None:
    if raw_mapping is None:
        return None
    if not isinstance(raw_mapping, dict):
        raise ValueError("field_mapping must be an object when provided")
    return {str(key): str(value) for key, value in raw_mapping.items()}


def _effective_field_mapping(field_mapping: dict[str, str] | None) -> dict[str, str]:
    return DEFAULT_FEISHU_FIELD_MAPPING | (field_mapping or {})


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


def _coerce_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _coerce_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)
