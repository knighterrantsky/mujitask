from __future__ import annotations

import mimetypes
import os
import re
from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from automation_business_scaffold.config import get_execution_control_defaults
from automation_business_scaffold.capabilities.input_sources.feishu.targets import (
    FeishuTableTarget,
)
from automation_business_scaffold.capabilities.input_sources.feishu.write_payloads import (
    coerce_int,
    first_non_empty,
    list_text,
    mapping,
    mapping_list,
    text,
)
from automation_business_scaffold.infrastructure.artifacts.artifact_store import (
    create_artifact_store,
    normalize_artifact_store_provider,
)


_FEISHU_ATTACHMENT_FIELD_TYPE = 17
_FEISHU_DATE_FIELD_TYPE = 5
_FEISHU_MULTI_SELECT_FIELD_TYPE = 4


def prepare_fields_for_write(
    fields: Mapping[str, Any],
    field_schema: Mapping[str, Mapping[str, Any]],
    *,
    client: Any,
    target: FeishuTableTarget,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    prepared: dict[str, Any] = {}
    for field_name, value in fields.items():
        name = text(field_name)
        if not name:
            continue
        if is_attachment_field(field_schema.get(name)):
            attachment_refs = attachment_file_token_ref_items(value, client=client, target=target, payload=payload)
            if attachment_refs:
                prepared[name] = attachment_refs
            continue
        if is_date_field(field_schema.get(name)):
            prepared_value = date_value_for_write(value)
            if prepared_value not in (None, ""):
                prepared[name] = prepared_value
            continue
        if is_multi_select_field(field_schema.get(name)):
            prepared_value = multi_select_value_for_write(value, field_schema.get(name))
            if prepared_value:
                prepared[name] = prepared_value
            continue
        prepared[name] = value
    return prepared


def is_attachment_field(field_schema: Mapping[str, Any] | None) -> bool:
    if not isinstance(field_schema, Mapping):
        return False
    field_type = field_schema.get("type")
    return field_type == _FEISHU_ATTACHMENT_FIELD_TYPE or text(field_type).lower() in {
        str(_FEISHU_ATTACHMENT_FIELD_TYPE),
        "attachment",
        "attachments",
    }


def is_date_field(field_schema: Mapping[str, Any] | None) -> bool:
    if not isinstance(field_schema, Mapping):
        return False
    field_type = field_schema.get("type")
    return field_type == _FEISHU_DATE_FIELD_TYPE or text(field_type).lower() in {
        str(_FEISHU_DATE_FIELD_TYPE),
        "date",
        "datetime",
    }


def is_multi_select_field(field_schema: Mapping[str, Any] | None) -> bool:
    if not isinstance(field_schema, Mapping):
        return False
    field_type = field_schema.get("type")
    return field_type == _FEISHU_MULTI_SELECT_FIELD_TYPE or text(field_type).lower() in {
        str(_FEISHU_MULTI_SELECT_FIELD_TYPE),
        "multi_select",
        "multiselect",
        "multiple_select",
    }


def multi_select_value_for_write(value: Any, field_schema: Mapping[str, Any] | None) -> list[str]:
    values = list_text(value)
    if not values:
        return []
    allowed = _multi_select_allowed_options(field_schema)
    result: list[str] = []
    for item in values:
        if allowed and item not in allowed:
            continue
        if item not in result:
            result.append(item)
    return result


def date_value_for_write(value: Any) -> int | str | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number * 1000 if 0 < number < 10_000_000_000 else number
    if isinstance(value, datetime):
        item = value if value.tzinfo is not None else value.replace(tzinfo=_feishu_date_timezone())
        return int(item.timestamp() * 1000)
    if isinstance(value, date):
        return _date_to_feishu_millis(value)
    item = text(value)
    if not item:
        return None
    if re.fullmatch(r"\d+", item):
        number = int(item)
        return number * 1000 if 0 < number < 10_000_000_000 else number
    parsed_date = _parse_date_only(item)
    if parsed_date is not None:
        return _date_to_feishu_millis(parsed_date)
    parsed_datetime = _parse_datetime(item)
    if parsed_datetime is not None:
        value_with_zone = parsed_datetime if parsed_datetime.tzinfo is not None else parsed_datetime.replace(tzinfo=_feishu_date_timezone())
        return int(value_with_zone.timestamp() * 1000)
    return item


def attachment_file_token_ref_items(
    value: Any,
    *,
    client: Any | None = None,
    target: FeishuTableTarget | None = None,
    payload: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    refs = []
    for item in attachment_write_items(value):
        file_token = first_non_empty(item.get("file_token"))
        if is_feishu_attachment_file_token(file_token):
            refs.append({"file_token": file_token})
            continue
        if client is not None and target is not None:
            uploaded_token = upload_attachment_item(client, target, item, payload=payload or {})
            if uploaded_token:
                refs.append({"file_token": uploaded_token})
    return _dedupe_ref_items(refs)


def attachment_write_items(value: Any) -> list[dict[str, str]]:
    values = value if isinstance(value, list) else [value]
    refs: list[dict[str, str]] = []
    for item in values:
        if isinstance(item, Mapping):
            refs.append(
                {
                    "file_token": first_non_empty(item.get("file_token")),
                    "url": first_non_empty(
                        item.get("url"),
                        item.get("source_url"),
                        item.get("tmp_url"),
                        item.get("download_url"),
                        item.get("link"),
                        item.get("remote" + "_uri"),
                    ),
                    "local_path": first_non_empty(item.get("local_path"), item.get("source_path"), item.get("path")),
                    "object" + "_key": first_non_empty(item.get("object" + "_key")),
                    "bucket": first_non_empty(item.get("bucket")),
                    "file_name": first_non_empty(item.get("file_name"), item.get("name")),
                    "mime_type": first_non_empty(item.get("mime_type"), item.get("type")),
                }
            )
            continue
        item_text = text(item)
        if item_text:
            refs.append({"url": item_text})
    return refs


def dedupe_attachment_write_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        file_token = text(item.get("file_token"))
        key = (
            ("file_token", file_token, "")
            if file_token
            else ("", text(item.get("url")), text(item.get("local_path") or item.get("object" + "_key")))
        )
        if not any(key) or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def is_feishu_attachment_file_token(value: Any) -> bool:
    token = text(value)
    if not token:
        return False
    if token.startswith(("tiktok_uri:", "s3://", "http://", "https://", "file://")):
        return False
    return not any(separator in token for separator in ("/", "\\", ":", "?"))


def upload_attachment_item(
    client: Any,
    target: FeishuTableTarget,
    item: Mapping[str, Any],
    *,
    payload: Mapping[str, Any],
) -> str:
    file_name = _attachment_file_name(item)
    local_path = _attachment_local_path(item)
    if local_path:
        file_data = local_path.read_bytes()
        file_name = file_name or local_path.name
        return client.upload_media(
            file_name=file_name,
            file_data=file_data,
            parent_node=target.app_token,
            extra=_attachment_upload_extra(target, payload),
        )

    bucket = first_non_empty(item.get("bucket"))
    object_key = first_non_empty(item.get("object_key"))
    if bucket or object_key:
        file_data = _read_materialized_attachment(bucket=bucket, object_key=object_key)
        return client.upload_media(
            file_name=file_name or Path(object_key).name or "attachment.bin",
            file_data=file_data,
            parent_node=target.app_token,
            extra=_attachment_upload_extra(target, payload),
        )

    url = first_non_empty(item.get("url"))
    if not url or url.startswith("s3://"):
        return ""
    parsed = urlparse(url)
    if parsed.scheme == "file":
        path = Path(parsed.path).expanduser()
        if path.exists() and path.is_file():
            file_data = path.read_bytes()
            return client.upload_media(
                file_name=file_name or path.name,
                file_data=file_data,
                parent_node=target.app_token,
                extra=_attachment_upload_extra(target, payload),
            )
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""

    timeout_seconds = coerce_int(
        first_non_empty(payload.get("attachment_download_timeout_seconds"), payload.get("download_timeout_seconds")),
        default=30,
        minimum=1,
        maximum=300,
    )
    request_pacer = getattr(client, "request_pacer", None)
    if request_pacer is not None:
        request_pacer.wait_before_request("feishu:attachment_download")
    try:
        response = requests.get(url, timeout=timeout_seconds, headers={"User-Agent": "Mozilla/5.0"})
    finally:
        if request_pacer is not None:
            request_pacer.mark_request_finished("feishu:attachment_download")
    response.raise_for_status()
    if not response.content:
        return ""
    content_type = first_non_empty(item.get("mime_type"), response.headers.get("Content-Type"))
    return client.upload_media(
        file_name=file_name or _attachment_file_name_from_url(url, content_type),
        file_data=response.content,
        parent_node=target.app_token,
        extra=_attachment_upload_extra(target, payload),
    )


def _read_materialized_attachment(*, bucket: str, object_key: str) -> bytes:
    if not bucket or not object_key:
        raise ValueError("Materialized Feishu attachment requires bucket and object_key.")
    defaults = get_execution_control_defaults()
    provider = normalize_artifact_store_provider(defaults.artifact_store_provider)
    if provider == "local" or not defaults.artifact_bucket:
        raise ValueError("Object storage is not configured for materialized attachment read.")
    if bucket != defaults.artifact_bucket:
        raise ValueError("Materialized attachment bucket is outside configured object storage.")
    prefix = defaults.artifact_object_prefix.strip("/")
    normalized_key = object_key.strip().lstrip("/")
    if prefix and not normalized_key.startswith(f"{prefix}/"):
        raise ValueError("Materialized attachment object_key is outside configured prefix.")
    store = create_artifact_store(
        {
            "artifact_store_provider": defaults.artifact_store_provider,
            "minio_endpoint": defaults.minio_endpoint,
            "minio_access_key": defaults.minio_access_key,
            "minio_secret_key": defaults.minio_secret_key,
            "minio_secure": defaults.minio_secure,
            "minio_region": defaults.minio_region,
            "minio_create_bucket": False,
        }
    )
    if store is None or not callable(getattr(store, "read_bytes", None)):
        raise ValueError("Configured object storage does not support attachment reads.")
    content = store.read_bytes(bucket=bucket, object_key=normalized_key)
    if not content:
        raise ValueError("Materialized attachment object is empty.")
    return content


def _multi_select_allowed_options(field_schema: Mapping[str, Any] | None) -> set[str]:
    schema = mapping(field_schema)
    property_payload = mapping(schema.get("property"))
    options = property_payload.get("options") or schema.get("options")
    allowed: set[str] = set()
    for option in mapping_list(options):
        name = first_non_empty(option.get("name"), option.get("text"), option.get("value"), option.get("id"))
        if name:
            allowed.add(name)
    return allowed


def _parse_date_only(value: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_datetime(value: str) -> datetime | None:
    normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _date_to_feishu_millis(value: date) -> int:
    item = datetime.combine(value, time.min, tzinfo=_feishu_date_timezone())
    return int(item.timestamp() * 1000)


def _feishu_date_timezone() -> timezone:
    zone_name = os.environ.get("FEISHU_DATE_TIMEZONE", "Asia/Shanghai")
    try:
        return ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=8))


def _attachment_local_path(item: Mapping[str, Any]) -> Path | None:
    path_text = first_non_empty(item.get("local_path"))
    if not path_text:
        return None
    path = Path(path_text).expanduser()
    if path.exists() and path.is_file():
        return path
    return None


def _attachment_file_name(item: Mapping[str, Any]) -> str:
    configured = first_non_empty(item.get("file_name"), item.get("name"))
    return Path(configured).name if configured else ""


def _attachment_file_name_from_url(url: str, content_type: str) -> str:
    path_name = Path(urlparse(url).path).name
    if path_name:
        return path_name
    suffix = mimetypes.guess_extension(str(content_type or "").split(";")[0].strip()) or ".bin"
    return f"attachment{suffix}"


def _attachment_upload_extra(target: FeishuTableTarget, payload: Mapping[str, Any]) -> dict[str, Any]:
    configured = mapping(payload.get("attachment_upload_extra"))
    if configured:
        return configured
    return {"bitablePerm": {"tableId": target.table_id}}


def _dedupe_ref_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (text(item.get("file_token")), text(item.get("url")))
        if not any(key) or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped
