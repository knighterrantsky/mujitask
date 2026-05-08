from __future__ import annotations

from typing import Any, Mapping

from automation_business_scaffold.capabilities.input_sources.feishu.field_envelopes import (
    attachment_write_items,
    dedupe_attachment_write_items,
    is_attachment_field,
    is_multi_select_field,
)
from automation_business_scaffold.capabilities.input_sources.feishu.targets import (
    FeishuTableTarget,
)
from automation_business_scaffold.capabilities.input_sources.feishu.write_payloads import (
    first_non_empty,
    list_text,
    mapping,
    text,
    text_value,
)


def execute_one_write(
    client: Any,
    target: FeishuTableTarget,
    record: Mapping[str, Any],
    *,
    field_schema: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], str, str]:
    op = text(record.get("op"))
    fields = mapping(record.get("fields"))
    record_id = text(record.get("record_id"))
    if op == "delete" and record_id:
        raw = client.delete_record(target.app_token, target.table_id, record_id)
        return raw, record_id, "delete"

    if op == "update" and record_id:
        raw = client.update_record(
            target.app_token,
            target.table_id,
            record_id,
            fields_for_update(
                record,
                fields,
                existing_fields=find_existing_record_fields(client, target, record_id),
                field_schema=field_schema or {},
            ),
        )
        return raw, record_id, "update"

    upsert_key = mapping(record.get("upsert_key"))
    if op == "upsert" and upsert_key:
        existing_row = find_existing_record(client, target, upsert_key)
        existing_id = text(existing_row.get("record_id") or existing_row.get("id"))
        if existing_id:
            raw = client.update_record(
                target.app_token,
                target.table_id,
                existing_id,
                fields_for_update(
                    record,
                    fields,
                    existing_fields=mapping(existing_row.get("fields")),
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
            fields_for_update(
                record,
                fields,
                existing_fields=find_existing_record_fields(client, target, record_id),
                field_schema=field_schema or {},
            ),
        )
        return raw, record_id, "update"

    if op in {"insert_if_absent", "create_if_absent"}:
        raw = client.create_record(target.app_token, target.table_id, fields)
        return raw, _response_record_id(raw), "append"

    raw = client.create_record(target.app_token, target.table_id, fields)
    return raw, _response_record_id(raw), "append"


def fields_for_update(
    record: Mapping[str, Any],
    fields: Mapping[str, Any],
    *,
    existing_fields: Mapping[str, Any] | None = None,
    field_schema: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    excluded = {text(value) for value in list(record.get("update_excluded_fields") or []) if text(value)}
    selected = {key: value for key, value in dict(fields).items() if text(key) not in excluded}
    return merge_update_fields(
        selected,
        existing_fields=mapping(existing_fields),
        field_schema=field_schema or {},
        replace_fields={text(value) for value in list(record.get("update_replace_fields") or []) if text(value)},
    )


def merge_update_fields(
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
        if text(field_name) in replace_field_names:
            merged[field_name] = value
            continue
        schema = field_schema.get(text(field_name))
        if is_attachment_field(schema):
            merged[field_name] = dedupe_attachment_write_items(
                attachment_write_items(existing_fields.get(field_name)) + attachment_write_items(value)
            )
            continue
        if is_multi_select_field(schema):
            merged[field_name] = _merge_text_lists(existing_fields.get(field_name), value)
            continue
        merged[field_name] = value
    return merged


def find_existing_record_id(
    client: Any,
    target: FeishuTableTarget,
    upsert_key: Mapping[str, Any],
) -> str:
    row = find_existing_record(client, target, upsert_key)
    return text(row.get("record_id") or row.get("id"))


def find_existing_record(
    client: Any,
    target: FeishuTableTarget,
    upsert_key: Mapping[str, Any],
) -> dict[str, Any]:
    field_name = text(upsert_key.get("field"))
    value = text(upsert_key.get("value"))
    if not field_name or not value:
        return {}
    try:
        rows = client.list_all_records(target.app_token, target.table_id, page_size=100, view_id=target.view_id or None)
    except AttributeError:
        return {}
    for row in rows:
        fields = mapping(row.get("fields"))
        if text_value(fields.get(field_name)) == value:
            return dict(row)
    return {}


def find_existing_record_fields(
    client: Any,
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
        if text(row.get("record_id") or row.get("id")) == record_id:
            return mapping(row.get("fields"))
    return {}


def _merge_text_lists(*values: Any) -> list[str]:
    merged: list[str] = []
    for value in values:
        for item in list_text(value):
            if item and item not in merged:
                merged.append(item)
    return merged


def _response_record_id(response: Mapping[str, Any]) -> str:
    data = mapping(response.get("data"))
    record = mapping(data.get("record"))
    return first_non_empty(data.get("record_id"), record.get("record_id"), record.get("id"))
