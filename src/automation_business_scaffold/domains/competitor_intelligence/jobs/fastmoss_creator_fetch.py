from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)

FASTMOSS_CREATOR_FETCH_JOB = JobDefinition(
    job_code="fastmoss_creator_fetch",
    handler_code="fastmoss_creator_fetch",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Fetch creator detail, creator facts, and related creator media context from FastMoss.",
    payload_contract=contract(
        "fastmoss_creator_fetch_payload",
        required_field("creator_identity", "Normalized creator or influencer business key.", type_hint="dict[str, Any]"),
        optional_field("source_context", "Source record, product, or parent job context.", type_hint="dict[str, Any]"),
        optional_field("detail_level", "Requested creator detail level.", type_hint="str"),
    ),
    result_contract=contract(
        "fastmoss_creator_fetch_result",
        required_field("creator_fact_bundle", "Normalized creator fact bundle.", type_hint="dict[str, Any]"),
        optional_field("product_relations", "Creator-to-product relation facts.", type_hint="list[dict[str, Any]]"),
        optional_field("media_refs", "Avatar or other media refs that may require sync.", type_hint="list[str]"),
    ),
    business_key_template="{creator_id}",
    dedupe_key_template="{request_id}:{stage_code}:{product_id_or_group}:{creator_id}",
    side_effects=("fastmoss", "runtime_db"),
)


JOB_DEFINITION = FASTMOSS_CREATOR_FETCH_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = ["HANDLER_CODE", "JOB_CODE", "JOB_DEFINITION", "FASTMOSS_CREATOR_FETCH_JOB"]
