from __future__ import annotations

from typing import Any, Mapping

from automation_business_scaffold.capabilities.input_sources.feishu.field_envelopes import (
    attachment_write_items,
    dedupe_attachment_write_items,
    is_attachment_field,
    is_feishu_attachment_file_token,
    is_multi_select_field,
    prepare_fields_for_write,
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


class EmptyPreparedFields(Exception):
    pass


def execute_one_write(
    client: Any,
    target: FeishuTableTarget,
    record: Mapping[str, Any],
    *,
    payload: Mapping[str, Any] | None = None,
    field_schema: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], str, str]:
    op = text(record.get("op"))
    fields = mapping(record.get("fields"))
    record_id = text(record.get("record_id"))
    schema = field_schema or {}
    settings = payload or {}
    if op == "delete" and record_id:
        raw = client.delete_record(target.app_token, target.table_id, record_id)
        return raw, record_id, "delete"

    if op == "update" and record_id:
        update_fields = fields_for_update(
            record,
            fields,
            existing_fields=find_existing_record_fields(client, target, record_id),
            field_schema=schema,
        )
        prepared_fields = _prepare_non_empty_fields(
            client,
            target,
            update_fields,
            field_schema=schema,
            payload=settings,
        )
        raw = client.update_record(
            target.app_token,
            target.table_id,
            record_id,
            prepared_fields,
        )
        return raw, record_id, "update"

    upsert_key = mapping(record.get("upsert_key"))
    if op == "upsert" and upsert_key:
        existing_row = find_existing_record(client, target, upsert_key)
        existing_id = text(existing_row.get("record_id") or existing_row.get("id"))
        if existing_id:
            update_fields = fields_for_update(
                record,
                fields,
                existing_fields=mapping(existing_row.get("fields")),
                field_schema=schema,
            )
            prepared_fields = _prepare_non_empty_fields(
                client,
                target,
                update_fields,
                field_schema=schema,
                payload=settings,
            )
            raw = client.update_record(
                target.app_token,
                target.table_id,
                existing_id,
                prepared_fields,
            )
            return raw, existing_id, "update"
        raw = client.create_record(
            target.app_token,
            target.table_id,
            _prepare_non_empty_fields(client, target, fields, field_schema=schema, payload=settings),
        )
        return raw, _response_record_id(raw), "append"

    if op == "upsert" and record_id:
        update_fields = fields_for_update(
            record,
            fields,
            existing_fields=find_existing_record_fields(client, target, record_id),
            field_schema=schema,
        )
        prepared_fields = _prepare_non_empty_fields(
            client,
            target,
            update_fields,
            field_schema=schema,
            payload=settings,
        )
        raw = client.update_record(
            target.app_token,
            target.table_id,
            record_id,
            prepared_fields,
        )
        return raw, record_id, "update"

    if op in {"insert_if_absent", "create_if_absent"}:
        raw = client.create_record(
            target.app_token,
            target.table_id,
            _prepare_non_empty_fields(client, target, fields, field_schema=schema, payload=settings),
        )
        return raw, _response_record_id(raw), "append"

    raw = client.create_record(
        target.app_token,
        target.table_id,
        _prepare_non_empty_fields(client, target, fields, field_schema=schema, payload=settings),
    )
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
    for field_name, value in mapping(record.get("update_accumulate_fields")).items():
        if text(field_name) in excluded:
            continue
        if text(field_name) in selected:
            selected[field_name] = value
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
        field_name_text = text(field_name)
        schema = field_schema.get(field_name_text)
        if field_name_text in replace_field_names:
            if is_attachment_field(schema) and _should_skip_uploaded_attachment_replace(existing_fields.get(field_name), value):
                continue
            merged[field_name] = value
            continue
        if is_attachment_field(schema):
            merged[field_name] = dedupe_attachment_write_items(
                attachment_write_items(existing_fields.get(field_name)) + attachment_write_items(value)
            )
            continue
        if is_multi_select_field(schema):
            merged[field_name] = _merge_text_lists(existing_fields.get(field_name), value)
            continue
        if text(field_name) == "关联商品销量":
            merged[field_name] = _merge_numeric_sum(existing_fields.get(field_name), value)
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
    filter_expr = _exact_match_filter_expr(field_name, value)
    try:
        return _find_existing_record_by_filter(client, target, filter_expr, field_name=field_name, value=value)
    except (AttributeError, TypeError):
        pass
    try:
        rows = _list_all_records(client, target, filter_expr=filter_expr)
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
        response = client.get_record(target.app_token, target.table_id, record_id)
        data = mapping(response.get("data"))
        record = mapping(data.get("record")) or data
        if record:
            return mapping(record.get("fields"))
    except AttributeError:
        pass
    try:
        rows = _list_all_records(client, target)
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


def _merge_numeric_sum(existing: Any, incoming: Any) -> str:
    incoming_number = _numeric_value(incoming)
    if incoming_number is None:
        return text_value(existing)
    existing_number = _numeric_value(existing) or 0.0
    return _format_trimmed_decimal(existing_number + incoming_number)


def _numeric_value(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    item = text_value(value).replace(",", "").replace("$", "").replace(" ", "")
    if not item:
        return None
    multiplier = 1.0
    lower = item.lower()
    for suffix, suffix_multiplier in (("亿", 100_000_000.0), ("万", 10_000.0), ("w", 10_000.0), ("m", 1_000_000.0), ("k", 1_000.0)):
        if lower.endswith(suffix):
            multiplier = suffix_multiplier
            item = item[: -len(suffix)]
            break
    try:
        return float(item) * multiplier
    except ValueError:
        return None


def _format_trimmed_decimal(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _response_record_id(response: Mapping[str, Any]) -> str:
    data = mapping(response.get("data"))
    record = mapping(data.get("record"))
    return first_non_empty(data.get("record_id"), record.get("record_id"), record.get("id"))


def _prepare_non_empty_fields(
    client: Any,
    target: FeishuTableTarget,
    fields: Mapping[str, Any],
    *,
    field_schema: Mapping[str, Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    prepared = prepare_fields_for_write(
        fields,
        field_schema,
        client=client,
        target=target,
        payload=payload,
    )
    if not prepared:
        raise EmptyPreparedFields("No Feishu fields remained after write preparation.")
    return prepared


def _find_existing_record_by_filter(
    client: Any,
    target: FeishuTableTarget,
    filter_expr: str,
    *,
    field_name: str,
    value: str,
) -> dict[str, Any]:
    response = client.list_records(
        target.app_token,
        target.table_id,
        page_size=1,
        filter_expr=filter_expr,
        page_token=None,
        view_id=target.view_id or None,
    )
    rows = _mapping_list(mapping(response.get("data")).get("items"))
    for row in rows:
        fields = mapping(row.get("fields"))
        if text_value(fields.get(field_name)) == value:
            return dict(row)
    return {}


def _list_all_records(
    client: Any,
    target: FeishuTableTarget,
    *,
    filter_expr: str = "",
) -> list[dict[str, Any]]:
    try:
        rows = client.list_all_records(
            target.app_token,
            target.table_id,
            page_size=100,
            filter_expr=filter_expr or None,
            view_id=target.view_id or None,
        )
    except TypeError:
        rows = client.list_all_records(
            target.app_token,
            target.table_id,
            page_size=100,
            view_id=target.view_id or None,
        )
    return _mapping_list(rows)


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _exact_match_filter_expr(field_name: str, value: str) -> str:
    escaped_value = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'CurrentValue.[{field_name}] = "{escaped_value}"'


def _should_skip_uploaded_attachment_replace(existing: Any, incoming: Any) -> bool:
    if not attachment_write_items(existing):
        return False
    incoming_items = attachment_write_items(incoming)
    if not incoming_items:
        return False
    return not any(is_feishu_attachment_file_token(item.get("file_token")) for item in incoming_items)
