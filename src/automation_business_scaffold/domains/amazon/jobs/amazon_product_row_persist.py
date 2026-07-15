from __future__ import annotations

from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)


AMAZON_PRODUCT_ROW_PERSIST_JOB = JobDefinition(
    job_code="amazon_product_row_persist",
    handler_code="amazon_product_row_persist",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose=(
        "Serially materialize Amazon media, persist Amazon facts, and update the same "
        "source Feishu record."
    ),
    payload_contract=contract(
        "amazon_product_row_persist_payload",
        required_field("table_ref", "Configured Feishu table alias.", type_hint="str"),
        required_field("source_record_id", "Source Feishu record id.", type_hint="str"),
        required_field(
            "source_table_identity",
            "Resolved Feishu base and table identity for the Fact binding.",
            type_hint="dict[str, str]",
        ),
        required_field("requested_asin", "Normalized requested Amazon ASIN.", type_hint="str"),
        required_field("run_id", "Stable collection run id.", type_hint="str"),
        required_field(
            "collection_status",
            "Browser collection terminal business status.",
            type_hint="str",
        ),
        required_field(
            "normalized_capture_ref",
            "Governed normalized capture object reference.",
            type_hint="dict[str, Any]",
        ),
        required_field(
            "raw_capture_refs",
            "Governed raw evidence object references.",
            type_hint="list[dict[str, Any]]",
        ),
        optional_field("resolved_asin", "Resolved page ASIN.", type_hint="str"),
        optional_field(
            "media_source_refs",
            "Observed Amazon media awaiting materialization.",
            type_hint="list[dict[str, Any]]",
        ),
        optional_field(
            "field_coverage",
            "Compact browser field coverage summary.",
            type_hint="dict[str, Any]",
        ),
        optional_field(
            "browser_provider_name",
            "Non-sensitive browser provider code.",
            type_hint="str",
        ),
        optional_field(
            "stage_durations_ms",
            "Measured browser-stage durations awaiting row convergence timings.",
            type_hint="dict[str, float]",
        ),
    ),
    result_contract=contract(
        "amazon_product_row_persist_result",
        required_field("row_status", "Final Amazon row business status.", type_hint="str"),
        required_field("source_record_id", "Updated source Feishu record id.", type_hint="str"),
        required_field("requested_asin", "Requested Amazon ASIN.", type_hint="str"),
        required_field("run_id", "Stable collection run id.", type_hint="str"),
        required_field(
            "step_statuses",
            "Compact statuses for media, fact, and Feishu convergence.",
            type_hint="dict[str, str]",
        ),
        optional_field(
            "fact_refs",
            "Compact persisted product, snapshot, binding, and artifact references.",
            type_hint="dict[str, Any]",
        ),
        optional_field(
            "media_coverage",
            "Compact expected/materialized/missing media counts.",
            type_hint="dict[str, Any]",
        ),
        optional_field(
            "writeback",
            "Compact Feishu write counts and target record ids.",
            type_hint="dict[str, Any]",
        ),
        optional_field(
            "observability",
            "Sanitized row timings, coverage, counts, final status, and error code.",
            type_hint="dict[str, Any]",
        ),
    ),
    business_key_template="{source_record_id}:{requested_asin}",
    dedupe_key_template=("{request_id}:amazon_persist:{source_record_id}:{requested_asin}"),
    side_effects=("object_store", "fact_db", "feishu.write"),
)


JOB_DEFINITION = AMAZON_PRODUCT_ROW_PERSIST_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


def amazon_product_row_persist_handler(context: HandlerContext) -> HandlerResult:
    from automation_business_scaffold.domains.amazon.flows.amazon_product_row_persist.orchestrator import (
        run_amazon_product_row_persist_flow,
    )

    result = run_amazon_product_row_persist_flow(context)
    if result.handler_code != HANDLER_CODE:
        raise AssertionError(
            f"amazon_product_row_persist returned handler_code {result.handler_code!r}."
        )
    return result


__all__ = [
    "AMAZON_PRODUCT_ROW_PERSIST_JOB",
    "CONTRACT",
    "HANDLER_CODE",
    "JOB_CODE",
    "JOB_DEFINITION",
    "amazon_product_row_persist_handler",
]
