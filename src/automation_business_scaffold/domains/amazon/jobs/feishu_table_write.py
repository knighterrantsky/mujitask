from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    required_field,
)


FEISHU_TABLE_WRITE_JOB = JobDefinition(
    job_code="feishu_table_write",
    handler_code="feishu_table_write",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Write an Amazon collection status transition to the same Feishu source row.",
    payload_contract=contract(
        "amazon_collection_status_write_payload",
        required_field("target_table_ref", "Configured Amazon table alias.", type_hint="str"),
        required_field("source_record_id", "Exact Feishu source record id.", type_hint="str"),
        required_field("row_status", "Amazon row status transition.", type_hint="str"),
        required_field(
            "writeback_kind",
            "Stage or terminal status writeback discriminator.",
            type_hint="str",
        ),
        required_field(
            "error_code",
            "Stable redacted failure code; empty for non-terminal transitions.",
            type_hint="str",
        ),
        required_field("records", "One status-only projection record.", type_hint="list[dict[str, Any]]"),
    ),
    result_contract=contract(
        "amazon_collection_status_write_result",
        required_field("written_count", "Number of source rows updated.", type_hint="int"),
        required_field("skipped_count", "Number of source rows skipped.", type_hint="int"),
        required_field("failed_count", "Number of source rows that failed.", type_hint="int"),
        required_field(
            "target_record_ids",
            "Feishu record ids updated by the status projection.",
            type_hint="list[str]",
        ),
    ),
    business_key_template="{source_record_id}:{writeback_kind}:{row_status}",
    dedupe_key_template=(
        "{request_id}:{writeback_kind}:{stage_code}:{source_record_id}:{row_status}:{error_code}"
    ),
    side_effects=("feishu.write", "runtime_db"),
)


JOB_DEFINITION = FEISHU_TABLE_WRITE_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = ["FEISHU_TABLE_WRITE_JOB", "HANDLER_CODE", "JOB_CODE", "JOB_DEFINITION"]
