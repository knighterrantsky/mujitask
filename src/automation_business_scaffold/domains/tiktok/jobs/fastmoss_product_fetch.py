from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)

FASTMOSS_PRODUCT_FETCH_JOB = JobDefinition(
    job_code="fastmoss_product_fetch",
    handler_code="fastmoss_product_fetch",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Fetch normalized FastMoss product facts, metrics, and optional relation detail.",
    payload_contract=contract(
        "fastmoss_product_fetch_payload",
        required_field("product_identity", "Normalized business key for the product.", type_hint="dict[str, Any]"),
        optional_field("detail_level", "Requested detail level, such as related_creators.", type_hint="str"),
        optional_field("source_context", "Source row or request context.", type_hint="dict[str, Any]"),
    ),
    result_contract=contract(
        "fastmoss_product_fetch_result",
        optional_field("product_fact_bundle", "Normalized product/store metric bundle.", type_hint="dict[str, Any]"),
        optional_field("related_creators", "Related creator candidates for influencer fan-out.", type_hint="list[dict[str, Any]]"),
        optional_field("metrics_snapshot", "Observation snapshot for later fact upsert.", type_hint="dict[str, Any]"),
    ),
    business_key_template="{product_id_or_fastmoss_key}",
    dedupe_key_template="{request_id}:{stage_code}:{product_id_or_fastmoss_key}",
    side_effects=("fastmoss", "runtime_db"),
)


JOB_DEFINITION = FASTMOSS_PRODUCT_FETCH_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = ["HANDLER_CODE", "JOB_CODE", "JOB_DEFINITION", "FASTMOSS_PRODUCT_FETCH_JOB"]
