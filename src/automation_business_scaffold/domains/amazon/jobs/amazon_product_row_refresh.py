from __future__ import annotations

from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)


AMAZON_PRODUCT_ROW_REFRESH_JOB = JobDefinition(
    job_code="amazon_product_row_refresh",
    handler_code="amazon_product_row_refresh",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose=(
        "Refresh one Amazon source row, suspend for primary browser collection, and "
        "resume the same job to persist facts and Feishu projection."
    ),
    payload_contract=contract(
        "amazon_product_row_refresh_payload",
        required_field("source_record_id", "Source Feishu record id.", type_hint="str"),
        required_field("requested_asin", "Normalized Amazon US ASIN.", type_hint="str"),
        required_field("canonical_url", "Canonical Amazon US product URL.", type_hint="str"),
        required_field("table_ref", "Configured Amazon Feishu table alias.", type_hint="str"),
        required_field(
            "source_table_identity",
            "Resolved Feishu base and table identity.",
            type_hint="dict[str, str]",
        ),
        required_field(
            "runtime_context",
            "Submit-time browser target and artifact-storage snapshot.",
            type_hint="dict[str, str]",
        ),
        optional_field(
            "browser_execution",
            "Compact terminal browser execution supplied when the same row job resumes.",
            type_hint="dict[str, Any]",
        ),
    ),
    result_contract=contract(
        "amazon_product_row_refresh_result",
        required_field("source_record_id", "Source Feishu record id.", type_hint="str"),
        required_field("requested_asin", "Requested Amazon ASIN.", type_hint="str"),
        required_field("row_status", "Current or terminal row status.", type_hint="str"),
        optional_field(
            "browser_required",
            "True only while the row is suspended for primary browser collection.",
            type_hint="bool",
        ),
        optional_field(
            "browser_request",
            "Compact browser execution request owned by the batch executor.",
            type_hint="dict[str, Any]",
        ),
        optional_field(
            "step_statuses",
            "Compact row persistence step statuses.",
            type_hint="dict[str, str]",
        ),
        optional_field("fact_refs", "Persisted fact references.", type_hint="dict[str, Any]"),
        optional_field("writeback", "Compact Feishu write outcome.", type_hint="dict[str, Any]"),
    ),
    business_key_template="{source_record_id}:{requested_asin}",
    dedupe_key_template="{request_id}:amazon_row_refresh:{source_record_id}:{requested_asin}",
    side_effects=("browser", "object_store", "fact_db", "feishu.write", "runtime_db"),
)


JOB_DEFINITION = AMAZON_PRODUCT_ROW_REFRESH_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


def amazon_product_row_refresh_handler(context: HandlerContext) -> HandlerResult:
    from automation_business_scaffold.domains.amazon.flows.amazon_product_row_refresh.orchestrator import (
        run_amazon_product_row_refresh_flow,
    )

    result = run_amazon_product_row_refresh_flow(context)
    if result.handler_code != HANDLER_CODE:
        raise AssertionError(
            f"amazon_product_row_refresh returned handler_code {result.handler_code!r}."
        )
    return result


__all__ = [
    "AMAZON_PRODUCT_ROW_REFRESH_JOB",
    "CONTRACT",
    "HANDLER_CODE",
    "JOB_CODE",
    "JOB_DEFINITION",
    "amazon_product_row_refresh_handler",
]
