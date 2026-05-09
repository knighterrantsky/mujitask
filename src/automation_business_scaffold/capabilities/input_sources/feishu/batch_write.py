from __future__ import annotations

from typing import Any, Mapping

from automation_business_scaffold.capabilities.input_sources.feishu.field_envelopes import (
    prepare_fields_for_write,
)
from automation_business_scaffold.capabilities.input_sources.feishu.row_updates import (
    execute_one_write,
    find_existing_record_id,
)
from automation_business_scaffold.capabilities.input_sources.feishu.schema_normalization import (
    load_field_schema,
)
from automation_business_scaffold.capabilities.input_sources.feishu.targets import (
    FeishuTableTarget,
)
from automation_business_scaffold.capabilities.input_sources.feishu.transport_errors import (
    classify_feishu_exception,
)
from automation_business_scaffold.capabilities.input_sources.feishu.write_payloads import (
    coerce_int,
    compact_raw_result,
    first_non_empty,
    mapping,
    normalize_write_record,
    raw_batch_ref,
    raw_result_ref,
    text,
    write_record_key,
    write_result_record,
)


def execute_write_records(
    client: Any,
    target: FeishuTableTarget,
    records: list[Mapping[str, Any]],
    payload: Mapping[str, Any],
    *,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    batch_size = coerce_int(mapping(payload.get("write_policy")).get("batch_size"), default=50, minimum=1, maximum=500)
    del batch_size
    result_records: list[dict[str, Any]] = []
    target_record_ids: list[str] = []
    seen_keys: set[str] = set()
    written_count = 0
    skipped_count = 0
    failed_count = 0
    _emit_write_progress(progress_callback, "feishu_table_write.schema.start", "Loading Feishu field schema.")
    field_schema = load_field_schema(client, target)
    _emit_write_progress(
        progress_callback,
        "feishu_table_write.schema.done",
        f"Loaded Feishu field schema fields={len(field_schema)}.",
    )

    total_records = len(records)
    for index, record in enumerate(records, start=1):
        command = normalize_write_record(record, payload)
        _emit_write_progress(
            progress_callback,
            "feishu_table_write.record.prepare",
            _write_progress_message("Preparing Feishu write record", command, index=index, total=total_records),
        )
        command["fields"] = prepare_fields_for_write(
            mapping(command.get("fields")),
            field_schema,
            client=client,
            target=target,
            payload=payload,
        )
        record_key = write_record_key(command)
        if record_key and record_key in seen_keys:
            skipped_count += 1
            result_records.append(write_result_record(command, status="skipped", message="duplicate_write_command"))
            _emit_write_progress(
                progress_callback,
                "feishu_table_write.record.skipped",
                _write_progress_message("Skipped duplicate Feishu write record", command, index=index, total=total_records),
            )
            continue
        if record_key:
            seen_keys.add(record_key)

        op = text(command.get("op"))
        if op == "delete":
            if not text(command.get("record_id")):
                skipped_count += 1
                result_records.append(write_result_record(command, status="skipped", message="missing_record_id"))
                _emit_write_progress(
                    progress_callback,
                    "feishu_table_write.record.skipped",
                    _write_progress_message("Skipped Feishu delete without record_id", command, index=index, total=total_records),
                )
                continue
            try:
                _emit_write_progress(
                    progress_callback,
                    "feishu_table_write.record.write",
                    _write_progress_message("Deleting Feishu record", command, index=index, total=total_records),
                )
                raw_result, target_record_id, effective_op = execute_one_write(client, target, command, field_schema=field_schema)
            except Exception as exc:
                failed_count += 1
                classified = classify_feishu_exception(exc)
                result_records.append(
                    write_result_record(
                        command,
                        status="failed",
                        message=classified.message,
                        error_code=classified.error_code,
                        error_type=classified.error_type,
                    )
                )
                _emit_write_progress(
                    progress_callback,
                    "feishu_table_write.record.failed",
                    _write_progress_message(
                        f"Feishu delete failed error_code={classified.error_code}",
                        command,
                        index=index,
                        total=total_records,
                    ),
                )
                continue
            written_count += 1
            if target_record_id:
                target_record_ids.append(target_record_id)
            item = write_result_record(command, status="success", record_id=target_record_id, op=effective_op)
            item["raw_result"] = compact_raw_result(raw_result)
            result_records.append(item)
            _emit_write_progress(
                progress_callback,
                "feishu_table_write.record.done",
                _write_progress_message("Deleted Feishu record", command, index=index, total=total_records),
            )
            continue

        fields = mapping(command.get("fields"))
        if not fields:
            skipped_count += 1
            result_records.append(write_result_record(command, status="skipped", message="empty_fields"))
            _emit_write_progress(
                progress_callback,
                "feishu_table_write.record.skipped",
                _write_progress_message("Skipped empty Feishu write fields", command, index=index, total=total_records),
            )
            continue

        try:
            upsert_key = mapping(command.get("upsert_key"))
            if op in {"insert_if_absent", "create_if_absent"} and upsert_key:
                _emit_write_progress(
                    progress_callback,
                    "feishu_table_write.record.find_existing",
                    _write_progress_message("Finding existing Feishu record", command, index=index, total=total_records),
                )
                existing_id = find_existing_record_id(client, target, upsert_key)
                if existing_id:
                    skipped_count += 1
                    result_records.append(
                        write_result_record(
                            command,
                            status="skipped",
                            record_id=existing_id,
                            op="skip_existing",
                            message="existing_record",
                        )
                    )
                    _emit_write_progress(
                        progress_callback,
                        "feishu_table_write.record.skipped",
                        _write_progress_message(
                            f"Skipped existing Feishu record record_id={existing_id}",
                            command,
                            index=index,
                            total=total_records,
                        ),
                    )
                    continue
            _emit_write_progress(
                progress_callback,
                "feishu_table_write.record.write",
                _write_progress_message("Writing Feishu record", command, index=index, total=total_records),
            )
            raw_result, target_record_id, effective_op = execute_one_write(client, target, command, field_schema=field_schema)
        except Exception as exc:
            failed_count += 1
            classified = classify_feishu_exception(exc)
            result_records.append(
                write_result_record(
                    command,
                    status="failed",
                    message=classified.message,
                    error_code=classified.error_code,
                    error_type=classified.error_type,
                )
            )
            _emit_write_progress(
                progress_callback,
                "feishu_table_write.record.failed",
                _write_progress_message(
                    f"Feishu write failed error_code={classified.error_code}",
                    command,
                    index=index,
                    total=total_records,
                ),
            )
            continue

        written_count += 1
        if target_record_id:
            target_record_ids.append(target_record_id)
        item = write_result_record(command, status="success", record_id=target_record_id, op=effective_op)
        if mapping(payload.get("raw_capture_policy")).get("store_raw_response"):
            item["raw_result_ref"] = raw_result_ref(payload, target_record_id or command.get("business_entity_key"))
        item["raw_result"] = compact_raw_result(raw_result)
        result_records.append(item)
        _emit_write_progress(
            progress_callback,
            "feishu_table_write.record.done",
            _write_progress_message(
                f"Wrote Feishu record op={effective_op} record_id={target_record_id}",
                command,
                index=index,
                total=total_records,
            ),
        )

    return {
        "written_count": written_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "target_record_ids": target_record_ids,
        "records": result_records,
        "writeback_context": {
            "target_table_ref": text(payload.get("target_table_ref")),
            "mapper_code": text(payload.get("mapper_code")),
        },
        "raw_response_ref": raw_batch_ref(payload) if mapping(payload.get("raw_capture_policy")).get("store_raw_response") else "",
    }


def _emit_write_progress(callback: Any | None, progress_stage: str, message: str) -> None:
    if callable(callback):
        callback(progress_stage, message=message)


def _write_progress_message(prefix: str, command: Mapping[str, Any], *, index: int, total: int) -> str:
    upsert_key = mapping(command.get("upsert_key"))
    entity_key = first_non_empty(upsert_key.get("value"), command.get("business_entity_key"), command.get("record_id"))
    op = text(command.get("op"))
    return f"{prefix} {index}/{total} op={op} entity_key={entity_key}."
