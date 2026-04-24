from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)

FEISHU_TABLE_WRITE_JOB = JobDefinition(
    job_code="feishu_table_write",
    handler_code="feishu_table_write",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Write business projections back to a Feishu table through a projection mapper.",
    payload_contract=contract(
        "feishu_table_write_payload",
        required_field("target_table_ref", "Stable identifier for the target Feishu table.", type_hint="str"),
        required_field("records", "Normalized rows to insert or update.", type_hint="list[dict[str, Any]]"),
        optional_field("mapper_code", "Projection mapper applied before write.", type_hint="str"),
        optional_field("write_mode", "Insert, update, or upsert mode.", type_hint="str"),
        optional_field("idempotency_context", "Stable business identity for dedupe and checkpointing.", type_hint="dict[str, Any]"),
    ),
    result_contract=contract(
        "feishu_table_write_result",
        required_field("written_count", "Number of records written.", type_hint="int"),
        optional_field("target_record_ids", "Feishu record ids created or updated.", type_hint="list[str]"),
        optional_field("skipped_count", "Number of skipped rows.", type_hint="int"),
        optional_field("writeback_context", "Projection context for later summary or follow-up.", type_hint="dict[str, Any]"),
    ),
    business_key_template="{target_table_ref}:{business_entity_key}",
    dedupe_key_template="{request_id}:{job_code}:{target_table_ref}:{business_entity_key}",
    side_effects=("feishu", "runtime_db"),
)


JOB_DEFINITION = FEISHU_TABLE_WRITE_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = ["HANDLER_CODE", "JOB_CODE", "JOB_DEFINITION", "FEISHU_TABLE_WRITE_JOB"]
