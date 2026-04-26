from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)

PRODUCT_CREATOR_DISCOVERY_JOB = JobDefinition(
    job_code="product_creator_discovery",
    handler_code="product_creator_discovery",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Discover related creators for one competitor product as one business job.",
    payload_contract=contract(
        "product_creator_discovery_payload",
        required_field("product_identity", "Normalized business key for the product.", type_hint="dict[str, Any]"),
        optional_field("discovery_plan", "FastMoss product fetch plan.", type_hint="dict[str, Any]"),
        optional_field("relation_policy", "Creator sold/follower filter policy.", type_hint="dict[str, Any]"),
        optional_field("source_context", "Source row and product hit context.", type_hint="dict[str, Any]"),
    ),
    result_contract=contract(
        "product_creator_discovery_result",
        optional_field("product_fact_bundle", "Normalized product fact bundle.", type_hint="dict[str, Any]"),
        optional_field("normalized_creator_candidates", "Filtered creator candidates.", type_hint="list[dict[str, Any]]"),
        optional_field("product_hit_context", "Product-level discovery summary.", type_hint="dict[str, Any]"),
    ),
    business_key_template="{product_id_or_fastmoss_key}",
    dedupe_key_template="{request_id}:{stage_code}:{product_id_or_fastmoss_key}",
    side_effects=("fastmoss", "runtime_db"),
)


JOB_DEFINITION = PRODUCT_CREATOR_DISCOVERY_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = ["HANDLER_CODE", "JOB_CODE", "JOB_DEFINITION", "PRODUCT_CREATOR_DISCOVERY_JOB"]
