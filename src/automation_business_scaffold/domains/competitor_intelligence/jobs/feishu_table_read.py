from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)

FEISHU_TABLE_READ_JOB = JobDefinition(
    job_code="feishu_table_read",
    handler_code="feishu_table_read",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Read source rows or source snapshots from a Feishu table and normalize source context.",
    payload_contract=contract(
        "feishu_table_read_payload",
        required_field("source_table_ref", "Stable identifier for the source table or app table.", type_hint="str"),
        optional_field("view_ref", "Optional view identifier.", type_hint="str"),
        optional_field("filter_spec", "Normalized filter settings for the read.", type_hint="dict[str, Any]"),
        optional_field("adapter_code", "Source adapter used after transport-level read.", type_hint="str"),
        optional_field("cursor_context", "Existing stage cursor data for incremental reads.", type_hint="dict[str, Any]"),
    ),
    result_contract=contract(
        "feishu_table_read_result",
        required_field("source_rows", "Normalized source rows for executor fan-out.", type_hint="list[dict[str, Any]]"),
        optional_field("source_snapshot", "Source snapshot or metadata extracted from Feishu.", type_hint="dict[str, Any]"),
        optional_field("candidate_keys", "Candidate entity keys discovered from the rows.", type_hint="list[str]"),
        optional_field("writeback_context", "Context later reused by Feishu writeback stages.", type_hint="dict[str, Any]"),
    ),
    business_key_template="{source_table_ref}",
    dedupe_key_template="{request_id}:{job_code}:{source_table_ref}:{view_ref_or_default}",
    side_effects=("feishu", "runtime_db"),
)


JOB_DEFINITION = FEISHU_TABLE_READ_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = ["HANDLER_CODE", "JOB_CODE", "JOB_DEFINITION", "FEISHU_TABLE_READ_JOB"]
