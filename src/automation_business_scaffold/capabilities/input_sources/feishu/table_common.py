from __future__ import annotations

import mimetypes
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from automation_business_scaffold.infrastructure.feishu.api import (
    FeishuAPIError,
    FeishuBitableClient,
    parse_table_url,
)
from automation_business_scaffold.infrastructure.rate_limit import RequestPacer, resolve_api_request_pacer_config


@dataclass(frozen=True)
class FeishuTableTarget:
    access_token: str
    app_token: str
    table_id: str
    view_id: str = ""
    table_ref: str = ""
    table_url: str = ""


@dataclass(frozen=True)
class FeishuCommonError(Exception):
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

_COMPETITOR_AUTO_FIELDS = (
    "产品链接",
    "SKU-ID",
    "图片",
    "标题",
    "节日",
    "卖家",
    "价格",
    "Fastmoss价格",
    "昨日销量",
    "近7天销量",
    "近90天销量",
    "记录日期",
)
_COMPETITOR_WRITEBACK_EXCLUDED_FIELDS = {"商品状态"}
_FEISHU_ATTACHMENT_FIELD_TYPE = 17
_FEISHU_DATE_FIELD_TYPE = 5
_FEISHU_MULTI_SELECT_FIELD_TYPE = 4


def build_feishu_client(
    target: FeishuTableTarget,
    settings: Mapping[str, Any] | None = None,
) -> FeishuBitableClient:
    request_pacer = RequestPacer(resolve_api_request_pacer_config(settings, provider="feishu"))
    try:
        return FeishuBitableClient(target.access_token, request_pacer=request_pacer)
    except TypeError as exc:
        if "request_pacer" not in str(exc):
            raise
        return FeishuBitableClient(target.access_token)


def resolve_read_target(payload: Mapping[str, Any]) -> FeishuTableTarget:
    return _resolve_table_target(payload, table_ref_key="source_table_ref")


def resolve_write_target(payload: Mapping[str, Any]) -> FeishuTableTarget:
    return _resolve_table_target(payload, table_ref_key="target_table_ref")


def read_feishu_records(
    client: FeishuBitableClient,
    target: FeishuTableTarget,
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    inline_rows = _mapping_list(payload.get("raw_rows")) or _mapping_list(payload.get("records"))
    if inline_rows:
        return inline_rows, {"next_page_token": "", "has_more": False, "source": "inline"}

    pagination = _mapping(payload.get("pagination"))
    page_size = _coerce_int(pagination.get("page_size"), default=100, minimum=1, maximum=500)
    max_pages = _coerce_int(pagination.get("max_pages"), default=20, minimum=1, maximum=1000)
    page_token = _text(pagination.get("cursor") or pagination.get("page_token"))
    filter_expr = _render_filter_expr(payload.get("filter_spec"))
    view_id = _text(_mapping(payload.get("feishu_table")).get("view_id") or target.view_id or payload.get("view_id") or payload.get("view_ref"))

    rows: list[dict[str, Any]] = []
    has_more = False
    next_page_token = ""
    for _ in range(max_pages):
        response = client.list_records(
            target.app_token,
            target.table_id,
            page_size=page_size,
            filter_expr=filter_expr or None,
            page_token=page_token or None,
            view_id=view_id or None,
        )
        data = _mapping(response.get("data"))
        rows.extend(_mapping_list(data.get("items")))
        has_more = bool(data.get("has_more"))
        next_page_token = _text(data.get("page_token") or data.get("next_page_token"))
        if not has_more or not next_page_token:
            break
        page_token = next_page_token

    return rows, {"next_page_token": next_page_token if has_more else "", "has_more": has_more}


def normalize_raw_rows(
    records: list[Mapping[str, Any]],
    *,
    field_names: list[str],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    selected = [name for name in field_names if name]
    for record in records:
        fields = _mapping(record.get("fields"))
        if selected:
            fields = {name: fields.get(name) for name in selected if name in fields}
        normalized.append(
            {
                "record_id": _text(record.get("record_id") or record.get("id")),
                "fields": fields,
                "created_time": record.get("created_time") or record.get("created_at") or 0,
                "updated_time": (
                    record.get("updated_time")
                    or record.get("last_modified_time")
                    or record.get("modified_time")
                    or 0
                ),
            }
        )
    return normalized


def validate_read_schema(
    client: FeishuBitableClient,
    target: FeishuTableTarget,
    field_names: list[str],
) -> None:
    if not field_names:
        return
    available = _load_field_names(client, target)
    missing = sorted(name for name in field_names if name not in available)
    if missing:
        raise FeishuCommonError(
            error_type="schema_missing",
            error_code="feishu_field_missing",
            message="Feishu table is missing required fields.",
            retryable=False,
            details={"missing_fields": missing, "table_ref": target.table_ref},
        )


def validate_write_schema(
    client: FeishuBitableClient,
    target: FeishuTableTarget,
    records: list[Mapping[str, Any]],
) -> None:
    field_names: set[str] = set()
    for record in records:
        field_names.update(str(name) for name in _mapping(record.get("fields")))
    if not field_names:
        return
    available = _load_field_names(client, target)
    missing = sorted(name for name in field_names if name not in available)
    if missing:
        raise FeishuCommonError(
            error_type="schema_missing",
            error_code="feishu_field_missing",
            message="Feishu table is missing required write fields.",
            retryable=False,
            details={"missing_fields": missing, "table_ref": target.table_ref},
        )


def adapt_source_rows(
    raw_rows: list[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    adapter_code = _text(payload.get("adapter_code"))
    if not adapter_code:
        return {
            "source_rows": [],
            "candidate_keys": [],
            "adapter_summary": {
                "adapter_code": "",
                "input_row_count": len(raw_rows),
                "source_row_count": 0,
            },
        }
    from automation_business_scaffold.domains.tiktok.mappers.registry import (
        adapt_source_rows as run_source_adapter,
    )

    return run_source_adapter(adapter_code, raw_rows, payload)


def map_write_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    records = _mapping_list(payload.get("records"))
    if not records:
        from automation_business_scaffold.domains.tiktok.projections.registry import (
            selection_writeback_records,
        )

        records = selection_writeback_records(payload)
    mapper_code = _text(payload.get("mapper_code"))
    mapped: list[dict[str, Any]] = []
    for record in records:
        if _text(record.get("op")) == "delete":
            mapped.append(_normalize_write_record(record, payload))
            continue
        if _mapping(record.get("fields")):
            mapped.append(_normalize_write_record(record, payload))
            continue
        from automation_business_scaffold.domains.tiktok.projections.registry import (
            map_projection_record,
        )

        mapped.append(map_projection_record(mapper_code, record, payload))
    return [record for record in mapped if _mapping(record.get("fields")) or _text(record.get("op")) == "delete"]


def execute_write_records(
    client: FeishuBitableClient,
    target: FeishuTableTarget,
    records: list[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    batch_size = _coerce_int(_mapping(payload.get("write_policy")).get("batch_size"), default=50, minimum=1, maximum=500)
    del batch_size
    result_records: list[dict[str, Any]] = []
    target_record_ids: list[str] = []
    seen_keys: set[str] = set()
    written_count = 0
    skipped_count = 0
    failed_count = 0
    field_schema = _load_field_schema(client, target)

    for record in records:
        command = _normalize_write_record(record, payload)
        command["fields"] = _prepare_fields_for_write(
            _mapping(command.get("fields")),
            field_schema,
            client=client,
            target=target,
            payload=payload,
        )
        record_key = _write_record_key(command)
        if record_key and record_key in seen_keys:
            skipped_count += 1
            result_records.append(_write_result_record(command, status="skipped", message="duplicate_write_command"))
            continue
        if record_key:
            seen_keys.add(record_key)

        op = _text(command.get("op"))
        if op == "delete":
            if not _text(command.get("record_id")):
                skipped_count += 1
                result_records.append(_write_result_record(command, status="skipped", message="missing_record_id"))
                continue
            try:
                raw_result, target_record_id, effective_op = _execute_one_write(
                    client,
                    target,
                    command,
                    field_schema=field_schema,
                )
            except Exception as exc:
                failed_count += 1
                classified = classify_feishu_exception(exc)
                result_records.append(
                    _write_result_record(
                        command,
                        status="failed",
                        message=classified.message,
                        error_code=classified.error_code,
                        error_type=classified.error_type,
                    )
                )
                continue
            written_count += 1
            if target_record_id:
                target_record_ids.append(target_record_id)
            item = _write_result_record(command, status="success", record_id=target_record_id, op=effective_op)
            item["raw_result"] = _compact_raw_result(raw_result)
            result_records.append(item)
            continue

        fields = _mapping(command.get("fields"))
        if not fields:
            skipped_count += 1
            result_records.append(_write_result_record(command, status="skipped", message="empty_fields"))
            continue

        try:
            upsert_key = _mapping(command.get("upsert_key"))
            if op in {"insert_if_absent", "create_if_absent"} and upsert_key:
                existing_id = _find_existing_record_id(client, target, upsert_key)
                if existing_id:
                    skipped_count += 1
                    result_records.append(
                        _write_result_record(
                            command,
                            status="skipped",
                            record_id=existing_id,
                            op="skip_existing",
                            message="existing_record",
                        )
                    )
                    continue
            raw_result, target_record_id, effective_op = _execute_one_write(
                client,
                target,
                command,
                field_schema=field_schema,
            )
        except Exception as exc:
            failed_count += 1
            classified = classify_feishu_exception(exc)
            result_records.append(
                _write_result_record(
                    command,
                    status="failed",
                    message=classified.message,
                    error_code=classified.error_code,
                    error_type=classified.error_type,
                )
            )
            continue

        written_count += 1
        if target_record_id:
            target_record_ids.append(target_record_id)
        item = _write_result_record(command, status="success", record_id=target_record_id, op=effective_op)
        if _mapping(payload.get("raw_capture_policy")).get("store_raw_response"):
            item["raw_result_ref"] = _raw_result_ref(payload, target_record_id or command.get("business_entity_key"))
        item["raw_result"] = _compact_raw_result(raw_result)
        result_records.append(item)

    return {
        "written_count": written_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "target_record_ids": target_record_ids,
        "records": result_records,
        "writeback_context": {
            "target_table_ref": _text(payload.get("target_table_ref")),
            "mapper_code": _text(payload.get("mapper_code")),
        },
        "raw_response_ref": _raw_batch_ref(payload) if _mapping(payload.get("raw_capture_policy")).get("store_raw_response") else "",
    }


def classify_feishu_exception(exc: Exception) -> FeishuCommonError:
    if isinstance(exc, FeishuCommonError):
        return exc
    if all(hasattr(exc, name) for name in ("error_type", "error_code", "message", "retryable")):
        return FeishuCommonError(
            error_type=str(getattr(exc, "error_type")),
            error_code=str(getattr(exc, "error_code")),
            message=str(getattr(exc, "message")),
            retryable=bool(getattr(exc, "retryable")),
            details=dict(getattr(exc, "details") or {}),
        )
    if isinstance(exc, FeishuAPIError):
        message = str(exc)
        status = int(exc.status or 0)
        code = int(exc.code or 0)
        lowered = message.lower()
        details = {"status": exc.status, "code": exc.code}
        if status in {401, 403} or code in {99991663, 99991664}:
            return FeishuCommonError("auth_error", "feishu_auth_error", message, False, details)
        if status == 429 or code in {1254290, 99991400} or "rate" in lowered:
            return FeishuCommonError("rate_limited", "feishu_rate_limited", message, True, details)
        if status in {408, 504} or "timeout" in lowered or "timed out" in lowered:
            return FeishuCommonError("timeout", "feishu_timeout", message, True, details)
        if "field" in lowered or "schema" in lowered or "not exist" in lowered or "not found" in lowered:
            return FeishuCommonError("schema_missing", "feishu_schema_missing", message, False, details)
        if status >= 500 or status == 0:
            return FeishuCommonError("upstream_error", "feishu_upstream_error", message, True, details)
        return FeishuCommonError("upstream_error", "feishu_api_error", message, False, details)
    if isinstance(exc, (requests.exceptions.Timeout, TimeoutError)):
        return FeishuCommonError("timeout", "feishu_timeout", str(exc), True, {})
    if isinstance(exc, requests.exceptions.RequestException):
        return FeishuCommonError("upstream_error", "feishu_transport_error", str(exc), True, {})
    return FeishuCommonError("upstream_error", "feishu_unexpected_error", str(exc), True, {})


def _resolve_table_target(payload: Mapping[str, Any], *, table_ref_key: str) -> FeishuTableTarget:
    request_payload = _mapping(payload.get("request_payload"))
    table_ref = _text(payload.get(table_ref_key))
    table_payload = _resolve_table_payload(payload, request_payload, table_ref=table_ref)
    table_ref_url = table_ref if table_ref.startswith(("http://", "https://")) else ""
    table_url = _first_non_empty(
        table_payload.get("table_url"),
        table_ref_url,
        payload.get("source_table_url" if table_ref_key == "source_table_ref" else "target_table_url"),
        request_payload.get("source_table_url" if table_ref_key == "source_table_ref" else "target_table_url"),
        payload.get("table_url"),
        request_payload.get("table_url"),
    )

    parsed: dict[str, Any] = {}
    if table_url:
        try:
            parsed = dict(parse_table_url(table_url))
        except ValueError as exc:
            raise FeishuCommonError(
                error_type="configuration_error",
                error_code="invalid_table_url",
                message=str(exc),
                retryable=False,
                details={"table_url": table_url, "table_ref": table_ref},
            ) from exc

    app_token = _first_non_empty(
        table_payload.get("app_token"),
        table_payload.get("app_token_ref") if not _looks_like_secret_ref(table_payload.get("app_token_ref")) else "",
        parsed.get("app_token"),
        _resolve_secret_ref(table_payload.get("app_token_ref")),
    )
    table_id = _first_non_empty(table_payload.get("table_id"), parsed.get("table_id"))
    view_id = _first_non_empty(table_payload.get("view_id"), payload.get("view_id"), payload.get("view_ref"), parsed.get("view_id"))
    access_token = _resolve_access_token(payload, request_payload, table_payload)

    missing = []
    if not access_token:
        missing.append("access_token")
    if not app_token:
        missing.append("app_token")
    if not table_id:
        missing.append("table_id")
    if missing:
        raise FeishuCommonError(
            error_type="configuration_error",
            error_code="missing_feishu_table_target",
            message="Feishu table target could not be resolved.",
            retryable=False,
            details={"missing": missing, "table_ref": table_ref, "table_url": table_url},
        )

    return FeishuTableTarget(
        access_token=access_token,
        app_token=app_token,
        table_id=table_id,
        view_id=view_id,
        table_ref=table_ref,
        table_url=table_url,
    )


def _resolve_table_payload(
    payload: Mapping[str, Any],
    request_payload: Mapping[str, Any],
    *,
    table_ref: str,
) -> dict[str, Any]:
    table_payload = _mapping(payload.get("feishu_table"))
    if table_payload:
        return table_payload
    table_refs = _mapping(payload.get("table_refs")) or _mapping(request_payload.get("table_refs"))
    resolved = table_refs.get(table_ref)
    if isinstance(resolved, Mapping):
        return dict(resolved)
    if isinstance(resolved, str):
        return {"table_url": resolved}
    return {}


def _resolve_access_token(
    payload: Mapping[str, Any],
    request_payload: Mapping[str, Any],
    table_payload: Mapping[str, Any],
) -> str:
    access_token_env = _first_non_empty(
        table_payload.get("access_token_env"),
        payload.get("access_token_env"),
        request_payload.get("access_token_env"),
    )
    return _first_non_empty(
        table_payload.get("access_token"),
        payload.get("access_token"),
        payload.get("feishu_access_token"),
        request_payload.get("access_token"),
        request_payload.get("feishu_access_token"),
        os.environ.get(access_token_env, "") if access_token_env else "",
        _resolve_secret_ref(table_payload.get("access_token_ref")),
        os.environ.get("MUJITASK_FEISHU_ACCESS_TOKEN", ""),
    )


def _resolve_secret_ref(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    if text.startswith("env://"):
        return os.environ.get(text.removeprefix("env://"), "")
    if text.startswith("secret://"):
        suffix = re.sub(r"[^A-Za-z0-9]+", "_", text.rsplit("/", 1)[-1]).strip("_").upper()
        for env_name in (f"FEISHU_{suffix}", f"MUJITASK_FEISHU_{suffix}"):
            candidate = os.environ.get(env_name, "")
            if candidate:
                return candidate
    if text in os.environ:
        return os.environ.get(text, "")
    return ""


def _looks_like_secret_ref(value: Any) -> bool:
    text = _text(value)
    return text.startswith(("secret://", "env://"))


def _load_field_names(client: FeishuBitableClient, target: FeishuTableTarget) -> set[str]:
    return set(_load_field_schema(client, target))


def _load_field_schema(client: FeishuBitableClient, target: FeishuTableTarget) -> dict[str, dict[str, Any]]:
    try:
        fields = client.list_all_fields(target.app_token, target.table_id)
    except AttributeError:
        return {}
    schema: dict[str, dict[str, Any]] = {}
    for field in fields:
        if isinstance(field, Mapping):
            name = _text(field.get("field_name") or field.get("name"))
            if name:
                schema[name] = dict(field)
    return schema


def _prepare_fields_for_write(
    fields: Mapping[str, Any],
    field_schema: Mapping[str, Mapping[str, Any]],
    *,
    client: FeishuBitableClient,
    target: FeishuTableTarget,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    prepared: dict[str, Any] = {}
    for field_name, value in fields.items():
        name = _text(field_name)
        if not name:
            continue
        if _is_attachment_field(field_schema.get(name)):
            attachment_refs = _attachment_file_token_ref_items(
                value,
                client=client,
                target=target,
                payload=payload,
            )
            if attachment_refs:
                prepared[name] = attachment_refs
            continue
        if _is_date_field(field_schema.get(name)):
            prepared_value = _date_value_for_write(value)
            if prepared_value not in (None, ""):
                prepared[name] = prepared_value
            continue
        if _is_multi_select_field(field_schema.get(name)):
            prepared_value = _multi_select_value_for_write(value, field_schema.get(name))
            if prepared_value:
                prepared[name] = prepared_value
            continue
        prepared[name] = value
    return prepared


def _is_attachment_field(field_schema: Mapping[str, Any] | None) -> bool:
    if not isinstance(field_schema, Mapping):
        return False
    field_type = field_schema.get("type")
    return field_type == _FEISHU_ATTACHMENT_FIELD_TYPE or _text(field_type).lower() in {
        str(_FEISHU_ATTACHMENT_FIELD_TYPE),
        "attachment",
        "attachments",
    }


def _is_date_field(field_schema: Mapping[str, Any] | None) -> bool:
    if not isinstance(field_schema, Mapping):
        return False
    field_type = field_schema.get("type")
    return field_type == _FEISHU_DATE_FIELD_TYPE or _text(field_type).lower() in {
        str(_FEISHU_DATE_FIELD_TYPE),
        "date",
        "datetime",
    }


def _is_multi_select_field(field_schema: Mapping[str, Any] | None) -> bool:
    if not isinstance(field_schema, Mapping):
        return False
    field_type = field_schema.get("type")
    return field_type == _FEISHU_MULTI_SELECT_FIELD_TYPE or _text(field_type).lower() in {
        str(_FEISHU_MULTI_SELECT_FIELD_TYPE),
        "multi_select",
        "multiselect",
        "multiple_select",
    }


def _multi_select_value_for_write(value: Any, field_schema: Mapping[str, Any] | None) -> list[str]:
    values = _list_text(value)
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


def _multi_select_allowed_options(field_schema: Mapping[str, Any] | None) -> set[str]:
    schema = _mapping(field_schema)
    property_payload = _mapping(schema.get("property"))
    options = property_payload.get("options") or schema.get("options")
    allowed: set[str] = set()
    for option in _mapping_list(options):
        name = _first_non_empty(option.get("name"), option.get("text"), option.get("value"), option.get("id"))
        if name:
            allowed.add(name)
    return allowed


def _date_value_for_write(value: Any) -> int | str | None:
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
    text = _text(value)
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        number = int(text)
        return number * 1000 if 0 < number < 10_000_000_000 else number
    parsed_date = _parse_date_only(text)
    if parsed_date is not None:
        return _date_to_feishu_millis(parsed_date)
    parsed_datetime = _parse_datetime(text)
    if parsed_datetime is not None:
        item = parsed_datetime if parsed_datetime.tzinfo is not None else parsed_datetime.replace(tzinfo=_feishu_date_timezone())
        return int(item.timestamp() * 1000)
    return text


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


def _render_filter_expr(filter_spec: Any) -> str:
    if isinstance(filter_spec, str):
        return filter_spec.strip()
    spec = _mapping(filter_spec)
    return _first_non_empty(spec.get("filter_expr"), spec.get("filter"))


def _adapt_competitor_rows(raw_rows: list[Mapping[str, Any]], payload: Mapping[str, Any]) -> dict[str, Any]:
    spec = _mapping(payload.get("filter_spec"))
    skip_statuses = set(_list_text(spec.get("skip_product_status")))
    candidate_policy = _text(spec.get("candidate_policy"))
    auto_fields = tuple(_list_text(spec.get("auto_fields")) or _COMPETITOR_AUTO_FIELDS)
    snapshot_enabled = bool(_mapping(payload.get("snapshot_policy")).get("store_raw_rows"))
    source_rows: list[dict[str, Any]] = []
    skipped_complete = 0
    skipped_unavailable = 0
    dropped_empty = 0

    for row in raw_rows:
        fields = _mapping(row.get("fields"))
        identity = _product_identity_from_fields(fields)
        if not identity:
            dropped_empty += 1
            continue
        product_status = _field_text(fields, "商品状态", "product_status")
        if product_status and product_status in skip_statuses:
            skipped_unavailable += 1
            continue
        missing_auto_fields = [field for field in auto_fields if not _field_has_value(fields.get(field))]
        if candidate_policy == "missing_auto_maintained_fields" and not missing_auto_fields:
            skipped_complete += 1
            continue
        source_rows.append(
            _source_row(
                row,
                payload,
                identity=identity,
                business_fields={"product_status": product_status},
                extra={"missing_auto_fields": missing_auto_fields},
                snapshot_enabled=snapshot_enabled,
            )
        )

    return _adapter_result(
        source_rows,
        input_count=len(raw_rows),
        adapter_code="competitor_table_source_adapter",
        extra_summary={
            "skipped_complete_count": skipped_complete,
            "skipped_unavailable_count": skipped_unavailable,
            "dropped_empty_count": dropped_empty,
        },
    )


def _adapt_influencer_source_rows(raw_rows: list[Mapping[str, Any]], payload: Mapping[str, Any]) -> dict[str, Any]:
    spec = _mapping(payload.get("filter_spec"))
    skip_statuses = set(_list_text(spec.get("skip_product_status")))
    candidate_status = _list_text(spec.get("candidate_status"))
    snapshot_enabled = bool(_mapping(payload.get("snapshot_policy")).get("store_raw_rows"))
    source_rows: list[dict[str, Any]] = []
    skipped_status = 0
    skipped_unavailable = 0
    dropped_empty = 0

    for row in raw_rows:
        fields = _mapping(row.get("fields"))
        identity = _product_identity_from_fields(fields)
        if not identity:
            dropped_empty += 1
            continue
        product_status = _field_text(fields, "商品状态", "product_status")
        if product_status and product_status in skip_statuses:
            skipped_unavailable += 1
            continue
        influencer_status = _field_text(fields, "达人查找状态", "influencer_search_status")
        if candidate_status and influencer_status not in set(candidate_status):
            skipped_status += 1
            continue
        source_rows.append(
            _source_row(
                row,
                payload,
                identity=identity,
                business_fields={
                    "holiday": _field_text(fields, "节日", "holiday"),
                    "product_status": product_status,
                    "influencer_search_status": influencer_status,
                },
                snapshot_enabled=snapshot_enabled,
            )
        )

    return _adapter_result(
        source_rows,
        input_count=len(raw_rows),
        adapter_code="influencer_pool_source_adapter",
        extra_summary={
            "skipped_status_count": skipped_status,
            "skipped_unavailable_count": skipped_unavailable,
            "dropped_empty_count": dropped_empty,
        },
    )


def _adapt_selection_rows(raw_rows: list[Mapping[str, Any]], payload: Mapping[str, Any]) -> dict[str, Any]:
    selection_record_id = _text(payload.get("selection_record_id"))
    target_identity = _compact(
        {
            "product_id": _first_non_empty(payload.get("product_id"), _extract_product_id(payload.get("product_url"))),
            "product_url": payload.get("product_url"),
            "normalized_product_url": _normalize_product_url(payload.get("product_url")),
        }
    )
    snapshot_enabled = bool(_mapping(payload.get("snapshot_policy")).get("store_raw_rows"))
    source_rows: list[dict[str, Any]] = []
    dropped_empty = 0

    for row in raw_rows:
        record_id = _text(row.get("record_id"))
        if selection_record_id and record_id != selection_record_id:
            continue
        fields = _mapping(row.get("fields"))
        identity = _product_identity_from_fields(fields)
        if not identity:
            dropped_empty += 1
            continue
        if target_identity and not _identity_matches(identity, target_identity):
            continue
        source_rows.append(
            _source_row(
                row,
                payload,
                identity=identity,
                business_fields={"product_status": _field_text(fields, "商品状态", "product_status")},
                snapshot_enabled=snapshot_enabled,
            )
        )

    return _adapter_result(
        source_rows,
        input_count=len(raw_rows),
        adapter_code="selection_table_source_adapter",
        extra_summary={"dropped_empty_count": dropped_empty},
    )


def _source_row(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    business_fields: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    snapshot_enabled: bool,
) -> dict[str, Any]:
    record_id = _text(row.get("record_id"))
    source_table_ref = _text(payload.get("source_table_ref"))
    candidate_key = _candidate_key(identity)
    item = {
        "source_record_id": record_id,
        "source_table_ref": source_table_ref,
        "product_identity": dict(identity),
        "product_id": _text(identity.get("product_id")),
        "product_url": _text(identity.get("product_url")),
        "normalized_product_url": _text(identity.get("normalized_product_url")),
        "business_key": candidate_key,
        "business_fields": dict(business_fields or {}),
        "writeback_context": {
            "target_table_ref": _first_non_empty(payload.get("target_table_ref"), source_table_ref),
            "competitor_status_table_ref": _first_non_empty(payload.get("competitor_status_table_ref"), source_table_ref),
            "record_id": record_id,
        },
        "source_context": {
            "source_record_id": record_id,
            "source_table_ref": source_table_ref,
            "product_identity": dict(identity),
            "source_fields": _mapping(row.get("fields")),
        },
    }
    if snapshot_enabled:
        item["source_snapshot_ref"] = _raw_result_ref(payload, record_id)
    if extra:
        item.update(dict(extra))
    return _compact(item)


def _adapter_result(
    source_rows: list[dict[str, Any]],
    *,
    input_count: int,
    adapter_code: str,
    extra_summary: Mapping[str, Any],
) -> dict[str, Any]:
    deduped_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    deduped_count = 0
    for row in source_rows:
        key = _first_non_empty(row.get("business_key"), row.get("source_record_id"))
        if key and key in seen:
            deduped_count += 1
            continue
        if key:
            seen.add(key)
        deduped_rows.append(row)
    return {
        "source_rows": deduped_rows,
        "candidate_keys": [_candidate_key(row.get("product_identity")) for row in deduped_rows],
        "adapter_summary": {
            "adapter_code": adapter_code,
            "input_row_count": input_count,
            "source_row_count": len(deduped_rows),
            "deduped_count": deduped_count,
            **dict(extra_summary),
        },
    }


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
        "update_excluded_fields": list(record.get("update_excluded_fields") or payload.get("update_excluded_fields") or []),
        "update_replace_fields": list(record.get("update_replace_fields") or payload.get("update_replace_fields") or []),
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
        "备注": f"通过搜索关键字：{search_query}" if search_query else "",
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
        if name == "产品链接":
            normalized[name] = _link_value(_text_value(value)) if _text_value(value) else value
            continue
        if name in {"图片", "前台截图", "Fastmoss截图"}:
            if isinstance(value, Mapping) and any(
                _first_non_empty(value.get(key))
                for key in ("file_token", "local_path", "source_path", "path", "url", "source_url", "remote_uri", "object_key")
            ):
                normalized[name] = dict(value)
                continue
            normalized[name] = _raw_link_value(_text_value(value)) if _text_value(value) else value
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
        if field_name in {"记录日期", *_COMPETITOR_WRITEBACK_EXCLUDED_FIELDS}:
            continue
        if not _field_has_value(value):
            continue
        if not _field_has_value(existing_fields.get(field_name)):
            selected[field_name] = value
    if selected:
        selected["记录日期"] = projection_fields.get("记录日期") or date.today().isoformat()
    return selected


def _map_influencer_pool_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    creator_id = _first_non_empty(record.get("creator_id"), _mapping(record.get("creator_fact_bundle")).get("creator_id"))
    product_id = _text(record.get("product_id"))
    fields = _compact(
        {
            "达人ID": creator_id,
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
            "记录日期": date.today().isoformat(),
            "更新日期": date.today().isoformat(),
        }
    )
    return _normalize_write_record(
        {
            "op": "upsert",
            "business_entity_key": _first_non_empty(record.get("business_entity_key"), f"creator:{creator_id}" if creator_id else ""),
            "upsert_key": {"field": "达人ID", "value": creator_id} if creator_id else {},
            "fields": fields,
            "update_excluded_fields": ["记录日期"],
            "update_replace_fields": ["达人头像"],
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


def _attachment_file_token_ref_items(
    value: Any,
    *,
    client: FeishuBitableClient | None = None,
    target: FeishuTableTarget | None = None,
    payload: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    refs = []
    for item in _attachment_write_items(value):
        file_token = _first_non_empty(item.get("file_token"))
        if _is_feishu_attachment_file_token(file_token):
            refs.append({"file_token": file_token})
            continue
        if client is not None and target is not None:
            uploaded_token = _upload_attachment_item(client, target, item, payload=payload or {})
            if uploaded_token:
                refs.append({"file_token": uploaded_token})
    return _dedupe_ref_items(refs)


def _attachment_write_items(value: Any) -> list[dict[str, str]]:
    values = value if isinstance(value, list) else [value]
    refs: list[dict[str, str]] = []
    for item in values:
        if isinstance(item, Mapping):
            refs.append(
                {
                    "file_token": _first_non_empty(item.get("file_token")),
                    "url": _first_non_empty(
                        item.get("url"),
                        item.get("source_url"),
                        item.get("tmp_url"),
                        item.get("download_url"),
                        item.get("link"),
                        item.get("remote_uri"),
                    ),
                    "local_path": _first_non_empty(item.get("local_path"), item.get("source_path"), item.get("path")),
                    "object_key": _first_non_empty(item.get("object_key")),
                    "file_name": _first_non_empty(item.get("file_name"), item.get("name")),
                    "mime_type": _first_non_empty(item.get("mime_type"), item.get("type")),
                }
            )
            continue
        text = _text(item)
        if text:
            refs.append({"url": text})
    return refs


def _is_feishu_attachment_file_token(value: Any) -> bool:
    token = _text(value)
    if not token:
        return False
    if token.startswith(("tiktok_uri:", "s3://", "http://", "https://", "file://")):
        return False
    return not any(separator in token for separator in ("/", "\\", ":", "?"))


def _upload_attachment_item(
    client: FeishuBitableClient,
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

    url = _first_non_empty(item.get("url"))
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

    timeout_seconds = _coerce_int(
        _first_non_empty(payload.get("attachment_download_timeout_seconds"), payload.get("download_timeout_seconds")),
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
    content_type = _first_non_empty(item.get("mime_type"), response.headers.get("Content-Type"))
    return client.upload_media(
        file_name=file_name or _attachment_file_name_from_url(url, content_type),
        file_data=response.content,
        parent_node=target.app_token,
        extra=_attachment_upload_extra(target, payload),
    )


def _attachment_local_path(item: Mapping[str, Any]) -> Path | None:
    path_text = _first_non_empty(item.get("local_path"))
    if not path_text:
        return None
    path = Path(path_text).expanduser()
    if path.exists() and path.is_file():
        return path
    return None


def _attachment_file_name(item: Mapping[str, Any]) -> str:
    configured = _first_non_empty(item.get("file_name"), item.get("name"))
    return Path(configured).name if configured else ""


def _attachment_file_name_from_url(url: str, content_type: str) -> str:
    path_name = Path(urlparse(url).path).name
    if path_name:
        return path_name
    suffix = mimetypes.guess_extension(str(content_type or "").split(";")[0].strip()) or ".bin"
    return f"attachment{suffix}"


def _attachment_upload_extra(target: FeishuTableTarget, payload: Mapping[str, Any]) -> dict[str, Any]:
    configured = _mapping(payload.get("attachment_upload_extra"))
    if configured:
        return configured
    return {"bitablePerm": {"tableId": target.table_id}}


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


def _dedupe_attachment_write_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        file_token = _text(item.get("file_token"))
        key = ("file_token", file_token, "") if file_token else (
            "",
            _text(item.get("url")),
            _text(item.get("local_path") or item.get("object_key")),
        )
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

    shop_refs = _cooperation_shop_refs(record)
    shops_by_ref = _shops_by_ref(record)
    for shop_ref in shop_refs:
        shop = shops_by_ref.get(shop_ref)
        name = _first_non_empty(_mapping(shop).get("shop_name"), _mapping(shop).get("name"))
        if name and name not in names:
            names.append(name)

    if names:
        return names

    fact_bundle = _mapping(record.get("fact_bundle"))
    for relation in _mapping_list(_mapping(fact_bundle.get("relations")).get("shop_creators")):
        name = _first_non_empty(relation.get("shop_name"), _mapping(_mapping(relation.get("metadata")).get("raw")).get("shop_name"))
        if name and name not in names:
            names.append(name)
    return names


def _cooperation_shop_refs(record: Mapping[str, Any]) -> list[str]:
    refs: list[str] = []
    for relation in _mapping_list(record.get("relations")):
        if _text(relation.get("relation_type")) != "shop_collaborates_with_creator":
            continue
        ref = _strip_entity_ref(_first_non_empty(relation.get("from_entity_key"), relation.get("shop_key"), relation.get("shop_id"), relation.get("seller_id")))
        if ref and ref not in refs:
            refs.append(ref)
    fact_bundle = _mapping(record.get("fact_bundle"))
    for relation in _mapping_list(_mapping(fact_bundle.get("relations")).get("shop_creators")):
        ref = _strip_entity_ref(_first_non_empty(relation.get("shop_key"), relation.get("shop_id"), relation.get("seller_id"), relation.get("shop_name")))
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _shops_by_ref(record: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    shops: dict[str, Mapping[str, Any]] = {}
    for shop in _mapping_list(_mapping(record.get("entities")).get("shops")):
        for ref in _shop_refs(shop):
            shops.setdefault(ref, shop)
    for shop in _mapping_list(_mapping(record.get("fact_bundle")).get("shops")):
        for ref in _shop_refs(shop):
            shops.setdefault(ref, shop)
    return shops


def _shop_refs(shop: Mapping[str, Any]) -> list[str]:
    refs: list[str] = []
    for value in (shop.get("entity_key"), shop.get("shop_key"), shop.get("shop_id"), shop.get("seller_id"), shop.get("shop_name"), shop.get("name")):
        ref = _strip_entity_ref(value)
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _strip_entity_ref(value: Any) -> str:
    text = _text(value)
    if ":" in text:
        return text.split(":", 1)[1]
    return text


def _format_w_unit_display(value: Any) -> str:
    if value in (None, ""):
        return ""
    number = _number_value(value)
    if number is None:
        return _text(value)
    if abs(number) >= 10_000:
        sign = "-" if number < 0 else ""
        return f"{sign}{int(abs(number) / 10_000 + 0.5)}W"
    return "小于1W"


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
        "partial_success": "失败重试",
        "failed": "失败重试",
        "skipped": "跳过",
    }.get(status, status or "已完成")
    fields = {"达人查找状态": status_text}
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


def _execute_one_write(
    client: FeishuBitableClient,
    target: FeishuTableTarget,
    record: Mapping[str, Any],
    *,
    field_schema: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], str, str]:
    op = _text(record.get("op"))
    fields = _mapping(record.get("fields"))
    record_id = _text(record.get("record_id"))
    if op == "delete" and record_id:
        raw = client.delete_record(target.app_token, target.table_id, record_id)
        return raw, record_id, "delete"

    if op == "update" and record_id:
        raw = client.update_record(
            target.app_token,
            target.table_id,
            record_id,
            _fields_for_update(
                record,
                fields,
                existing_fields=_find_existing_record_fields(client, target, record_id),
                field_schema=field_schema or {},
            ),
        )
        return raw, record_id, "update"

    upsert_key = _mapping(record.get("upsert_key"))
    if op == "upsert" and upsert_key:
        existing_row = _find_existing_record(client, target, upsert_key)
        existing_id = _text(existing_row.get("record_id") or existing_row.get("id"))
        if existing_id:
            raw = client.update_record(
                target.app_token,
                target.table_id,
                existing_id,
                _fields_for_update(
                    record,
                    fields,
                    existing_fields=_mapping(existing_row.get("fields")),
                    field_schema=field_schema or {},
                ),
            )
            return raw, existing_id, "update"
        raw = client.create_record(target.app_token, target.table_id, fields)
        return raw, _response_record_id(raw), "append"

    if op == "upsert" and record_id:
        raw = client.update_record(
            target.app_token,
            target.table_id,
            record_id,
            _fields_for_update(
                record,
                fields,
                existing_fields=_find_existing_record_fields(client, target, record_id),
                field_schema=field_schema or {},
            ),
        )
        return raw, record_id, "update"

    if op in {"insert_if_absent", "create_if_absent"}:
        raw = client.create_record(target.app_token, target.table_id, fields)
        return raw, _response_record_id(raw), "append"

    raw = client.create_record(target.app_token, target.table_id, fields)
    return raw, _response_record_id(raw), "append"


def _fields_for_update(
    record: Mapping[str, Any],
    fields: Mapping[str, Any],
    *,
    existing_fields: Mapping[str, Any] | None = None,
    field_schema: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    excluded = {_text(value) for value in list(record.get("update_excluded_fields") or []) if _text(value)}
    selected = {key: value for key, value in dict(fields).items() if _text(key) not in excluded}
    return _merge_update_fields(
        selected,
        existing_fields=_mapping(existing_fields),
        field_schema=field_schema or {},
        replace_fields={_text(value) for value in list(record.get("update_replace_fields") or []) if _text(value)},
    )


def _merge_update_fields(
    fields: Mapping[str, Any],
    *,
    existing_fields: Mapping[str, Any],
    field_schema: Mapping[str, Mapping[str, Any]],
    replace_fields: set[str] | None = None,
) -> dict[str, Any]:
    if not existing_fields:
        return dict(fields)
    replace_field_names = replace_fields or set()
    merged: dict[str, Any] = {}
    for field_name, value in fields.items():
        if _text(field_name) in replace_field_names:
            merged[field_name] = value
            continue
        schema = field_schema.get(_text(field_name))
        if _is_attachment_field(schema):
            merged[field_name] = _dedupe_attachment_write_items(
                _attachment_write_items(existing_fields.get(field_name)) + _attachment_write_items(value)
            )
            continue
        if _is_multi_select_field(schema):
            merged[field_name] = _merge_text_lists(existing_fields.get(field_name), value)
            continue
        merged[field_name] = value
    return merged


def _merge_text_lists(*values: Any) -> list[str]:
    merged: list[str] = []
    for value in values:
        for item in _list_text(value):
            if item and item not in merged:
                merged.append(item)
    return merged


def _find_existing_record_id(
    client: FeishuBitableClient,
    target: FeishuTableTarget,
    upsert_key: Mapping[str, Any],
) -> str:
    field_name = _text(upsert_key.get("field"))
    value = _text(upsert_key.get("value"))
    if not field_name or not value:
        return ""
    try:
        rows = client.list_all_records(target.app_token, target.table_id, page_size=100, view_id=target.view_id or None)
    except AttributeError:
        return ""
    for row in rows:
        fields = _mapping(row.get("fields"))
        if _text_value(fields.get(field_name)) == value:
            return _text(row.get("record_id") or row.get("id"))
    return ""


def _find_existing_record(
    client: FeishuBitableClient,
    target: FeishuTableTarget,
    upsert_key: Mapping[str, Any],
) -> dict[str, Any]:
    field_name = _text(upsert_key.get("field"))
    value = _text(upsert_key.get("value"))
    if not field_name or not value:
        return {}
    try:
        rows = client.list_all_records(target.app_token, target.table_id, page_size=100, view_id=target.view_id or None)
    except AttributeError:
        return {}
    for row in rows:
        fields = _mapping(row.get("fields"))
        if _text_value(fields.get(field_name)) == value:
            return dict(row)
    return {}


def _find_existing_record_fields(
    client: FeishuBitableClient,
    target: FeishuTableTarget,
    record_id: str,
) -> dict[str, Any]:
    if not record_id:
        return {}
    try:
        rows = client.list_all_records(target.app_token, target.table_id, page_size=100, view_id=target.view_id or None)
    except AttributeError:
        return {}
    for row in rows:
        if _text(row.get("record_id") or row.get("id")) == record_id:
            return _mapping(row.get("fields"))
    return {}


def _response_record_id(response: Mapping[str, Any]) -> str:
    data = _mapping(response.get("data"))
    record = _mapping(data.get("record"))
    return _first_non_empty(data.get("record_id"), record.get("record_id"), record.get("id"))


def _write_record_key(record: Mapping[str, Any]) -> str:
    record_id = _text(record.get("record_id"))
    if record_id:
        return f"record:{record_id}"
    upsert_key = _mapping(record.get("upsert_key"))
    if upsert_key:
        return f"upsert:{_text(upsert_key.get('field'))}:{_text(upsert_key.get('value'))}"
    entity_key = _text(record.get("business_entity_key"))
    if entity_key:
        return f"entity:{entity_key}"
    return ""


def _write_result_record(
    record: Mapping[str, Any],
    *,
    status: str,
    record_id: str = "",
    op: str = "",
    message: str = "",
    error_type: str = "",
    error_code: str = "",
) -> dict[str, Any]:
    item = {
        "business_entity_key": _text(record.get("business_entity_key")),
        "record_id": _first_non_empty(record_id, record.get("record_id")),
        "op": _first_non_empty(op, record.get("op")),
        "status": status,
        "fields_written": list(_mapping(record.get("fields")).keys()),
    }
    if message:
        item["message"] = message
    if error_type:
        item["error_type"] = error_type
    if error_code:
        item["error_code"] = error_code
    return _compact(item)


def _raw_result_ref(payload: Mapping[str, Any], key: Any) -> str:
    namespace = _first_non_empty(
        _mapping(payload.get("snapshot_policy")).get("raw_snapshot_namespace"),
        "feishu/common",
    )
    request_id = _first_non_empty(payload.get("request_id"), payload.get("stage_code"), "request")
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", _text(key) or "row").strip("-") or "row"
    return f"artifact://{namespace}/{request_id}/{safe_key}.json"


def _raw_batch_ref(payload: Mapping[str, Any]) -> str:
    namespace = _first_non_empty(
        _mapping(payload.get("raw_capture_policy")).get("raw_response_namespace"),
        "feishu/common/write",
    )
    request_id = _first_non_empty(payload.get("request_id"), payload.get("stage_code"), "request")
    return f"artifact://{namespace}/{request_id}/batch-1.json"


def _compact_raw_result(raw_result: Mapping[str, Any]) -> dict[str, Any]:
    data = _mapping(raw_result.get("data"))
    if data:
        return {"code": raw_result.get("code", 0), "data": data}
    return {"code": raw_result.get("code", 0)}


def _product_identity_from_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    product_url = _field_text(fields, "产品链接", "商品链接", "product_url", "normalized_product_url")
    sku_id = _field_text(fields, "SKU-ID", "SKU ID", "商品ID", "product_id", "sku_id")
    product_id = _first_non_empty(_extract_product_id(sku_id), _extract_product_id(product_url))
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


def _raw_link_value(url: str) -> dict[str, str] | str:
    normalized = _text(url)
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
