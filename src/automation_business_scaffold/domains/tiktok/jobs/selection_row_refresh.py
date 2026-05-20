from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)
from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult

SELECTION_ROW_REFRESH_JOB = JobDefinition(
    job_code="selection_row_refresh",
    handler_code="selection_row_refresh",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Refresh one selection row as a serial pipeline that reuses the existing step handlers.",
    payload_contract=contract(
        "selection_row_refresh_payload",
        required_field("source_record_id", "Source Feishu record id for the selection row.", type_hint="str"),
        required_field("product_identity", "Normalized product identity for the selection row.", type_hint="dict[str, Any]"),
        optional_field("source_context", "Normalized row source context.", type_hint="dict[str, Any]"),
        optional_field("source_table_ref", "Source Feishu table reference used for writeback.", type_hint="str"),
        optional_field("target_table_ref", "Target Feishu table reference for writeback.", type_hint="str"),
        optional_field("request_payload", "Top-level workflow request payload for nested handler passthrough.", type_hint="dict[str, Any]"),
        optional_field("writeback_enabled", "Whether to write back to Feishu selection table.", type_hint="bool"),
    ),
    result_contract=contract(
        "selection_row_refresh_result",
        required_field("row_status", "Final status for the selection row pipeline.", type_hint="str"),
        required_field("step_timeline", "Ordered step execution results for the row pipeline.", type_hint="list[dict[str, Any]]"),
        optional_field("normalized_product_result", "Compact TikTok product identity/status summary used by downstream steps.", type_hint="dict[str, Any]"),
        optional_field("product_fact_bundle", "Compact FastMoss fact bundle counts and product identity for this row.", type_hint="dict[str, Any]"),
        optional_field("fact_upsert", "Compact Fact DB persistence outcome without fact_bundle payload.", type_hint="dict[str, Any]"),
        optional_field("writeback_projection", "Compact Feishu writeback projection fields for this row.", type_hint="dict[str, Any]"),
        optional_field("runtime_evidence", "Runtime evidence for request-first and browser fallback execution.", type_hint="dict[str, Any]"),
    ),
    business_key_template="{source_record_id_or_product_id}",
    dedupe_key_template="{request_id}:{stage_code}:{source_record_id_or_product_id}",
    side_effects=("runtime_db", "feishu.write", "fact_db.write", "artifact.write", "fastmoss.request", "tiktok.request"),
)


JOB_DEFINITION = SELECTION_ROW_REFRESH_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


def selection_row_refresh_handler(context: HandlerContext) -> HandlerResult:
    from automation_business_scaffold.domains.tiktok.flows.selection_row_refresh.orchestrator import (
        run_selection_row_refresh_flow,
    )

    result = run_selection_row_refresh_flow(context)
    if result.handler_code != HANDLER_CODE:
        raise AssertionError(
            f"selection_row_refresh returned handler_code {result.handler_code!r}."
        )
    return result


__all__ = [
    "SELECTION_ROW_REFRESH_JOB",
    "CONTRACT",
    "HANDLER_CODE",
    "JOB_CODE",
    "JOB_DEFINITION",
    "selection_row_refresh_handler",
]
