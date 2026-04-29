from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)

FASTMOSS_PRODUCT_SEARCH_JOB = JobDefinition(
    job_code="fastmoss_product_search",
    handler_code="fastmoss_product_search",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Search FastMoss products from normalized keyword or filter inputs.",
    payload_contract=contract(
        "fastmoss_product_search_payload",
        required_field("search_query", "Keyword or normalized product search query.", type_hint="str"),
        optional_field("filters", "Normalized FastMoss search filters.", type_hint="dict[str, Any]"),
        optional_field("limit", "Requested maximum candidate count; 0 means unlimited until pagination stops.", type_hint="int"),
        optional_field("condition_context", "Output condition context consumed by executor.", type_hint="dict[str, Any]"),
    ),
    result_contract=contract(
        "fastmoss_product_search_result",
        required_field("candidates", "Normalized candidate product list.", type_hint="list[dict[str, Any]]"),
        optional_field("raw_response_ref", "Artifact ref for the raw FastMoss response.", type_hint="str"),
        optional_field("condition_context", "Condition context returned for candidate processing.", type_hint="dict[str, Any]"),
    ),
    business_key_template="{search_query}",
    dedupe_key_template="{request_id}:{job_code}:{search_digest}",
    side_effects=("fastmoss", "runtime_db"),
)


JOB_DEFINITION = FASTMOSS_PRODUCT_SEARCH_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = ["HANDLER_CODE", "JOB_CODE", "JOB_DEFINITION", "FASTMOSS_PRODUCT_SEARCH_JOB"]
