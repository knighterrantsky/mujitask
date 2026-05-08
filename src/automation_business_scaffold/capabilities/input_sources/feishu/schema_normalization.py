from __future__ import annotations

from typing import Any, Mapping

from automation_business_scaffold.capabilities.input_sources.feishu.targets import (
    FeishuCommonError,
    FeishuTableTarget,
)


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
    client: Any,
    target: FeishuTableTarget,
    field_names: list[str],
) -> None:
    if not field_names:
        return
    available = load_field_names(client, target)
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
    client: Any,
    target: FeishuTableTarget,
    records: list[Mapping[str, Any]],
) -> None:
    field_names: set[str] = set()
    for record in records:
        field_names.update(str(name) for name in _mapping(record.get("fields")))
    if not field_names:
        return
    available = load_field_names(client, target)
    missing = sorted(name for name in field_names if name not in available)
    if missing:
        raise FeishuCommonError(
            error_type="schema_missing",
            error_code="feishu_field_missing",
            message="Feishu table is missing required write fields.",
            retryable=False,
            details={"missing_fields": missing, "table_ref": target.table_ref},
        )


def load_field_names(client: Any, target: FeishuTableTarget) -> set[str]:
    return set(load_field_schema(client, target))


def load_field_schema(client: Any, target: FeishuTableTarget) -> dict[str, dict[str, Any]]:
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


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()
