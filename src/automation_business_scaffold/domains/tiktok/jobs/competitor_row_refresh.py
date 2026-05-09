from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)
from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult

COMPETITOR_ROW_REFRESH_JOB = JobDefinition(
    job_code="competitor_row_refresh",
    handler_code="competitor_row_refresh",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Refresh one competitor row as a serial pipeline that reuses the existing step handlers.",
    payload_contract=contract(
        "competitor_row_refresh_payload",
        required_field("source_record_id", "Source Feishu record id for the competitor row.", type_hint="str"),
        required_field("product_identity", "Normalized product identity for the competitor row.", type_hint="dict[str, Any]"),
        optional_field("source_context", "Normalized row source context.", type_hint="dict[str, Any]"),
        optional_field("source_table_ref", "Source Feishu table reference used for writeback.", type_hint="str"),
        optional_field("request_payload", "Top-level workflow request payload for nested handler passthrough.", type_hint="dict[str, Any]"),
    ),
    result_contract=contract(
        "competitor_row_refresh_result",
        required_field("row_status", "Final status for the competitor row pipeline.", type_hint="str"),
        required_field("step_timeline", "Ordered step execution results for the row pipeline.", type_hint="list[dict[str, Any]]"),
        optional_field("normalized_product_result", "Effective TikTok product result used by downstream steps.", type_hint="dict[str, Any]"),
        optional_field("product_fact_bundle", "Effective FastMoss fact bundle for this row.", type_hint="dict[str, Any]"),
        optional_field("fact_upsert", "Fact DB persistence outcome.", type_hint="dict[str, Any]"),
        optional_field("writeback_projection", "Feishu writeback projection fields for this row.", type_hint="dict[str, Any]"),
        optional_field("runtime_evidence", "Runtime evidence for request-first and browser fallback execution.", type_hint="dict[str, Any]"),
    ),
    business_key_template="{source_record_id_or_product_id}",
    dedupe_key_template="{request_id}:{stage_code}:{source_record_id_or_product_id}",
    side_effects=("runtime_db", "feishu.write", "fact_db.write", "artifact.write", "fastmoss.request", "tiktok.request"),
)


JOB_DEFINITION = COMPETITOR_ROW_REFRESH_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


def competitor_row_refresh_handler(context: HandlerContext) -> HandlerResult:
    from automation_business_scaffold.domains.tiktok.flows.competitor_row_refresh.orchestrator import (
        run_competitor_row_refresh_flow,
    )

    result = run_competitor_row_refresh_flow(context)
    if result.handler_code != HANDLER_CODE:
        raise AssertionError(
            f"competitor_row_refresh returned handler_code {result.handler_code!r}."
        )
    return result


__all__ = [
    "COMPETITOR_ROW_REFRESH_JOB",
    "CONTRACT",
    "HANDLER_CODE",
    "JOB_CODE",
    "JOB_DEFINITION",
    "competitor_row_refresh_handler",
]
