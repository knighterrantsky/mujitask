from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    required_field,
)


FEISHU_TABLE_READ_JOB = JobDefinition(
    job_code="feishu_table_read",
    handler_code="feishu_table_read",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Read and validate one Amazon product source row from Feishu.",
    payload_contract=contract(
        "amazon_feishu_table_read_payload",
        required_field("source_table_ref", "Configured Amazon table alias.", type_hint="str"),
        required_field("source_record_id", "Exact Feishu source record id.", type_hint="str"),
        required_field(
            "adapter_code",
            "Amazon product source adapter code.",
            type_hint="str",
        ),
    ),
    result_contract=contract(
        "amazon_feishu_table_read_result",
        required_field(
            "source_rows",
            "Validated Amazon source row contexts.",
            type_hint="list[dict[str, Any]]",
        ),
        required_field(
            "adapter_summary",
            "Single-row lookup and identity validation summary.",
            type_hint="dict[str, Any]",
        ),
        required_field(
            "source_table_identity",
            "Resolved Feishu base and table identity.",
            type_hint="dict[str, str]",
        ),
    ),
    business_key_template="{source_record_id}",
    dedupe_key_template="{request_id}:amazon_read:{source_record_id}",
    side_effects=("feishu.read", "runtime_db"),
)


JOB_DEFINITION = FEISHU_TABLE_READ_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = ["FEISHU_TABLE_READ_JOB", "HANDLER_CODE", "JOB_CODE", "JOB_DEFINITION"]
