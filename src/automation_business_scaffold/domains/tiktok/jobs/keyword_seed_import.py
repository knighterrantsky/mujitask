from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)


KEYWORD_SEED_IMPORT_JOB = JobDefinition(
    job_code="keyword_seed_import",
    handler_code="keyword_seed_import",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Search FastMoss products and write TK competitor seed rows in one business job.",
    payload_contract=contract(
        "keyword_seed_import_payload",
        required_field("search_request", "Structured keyword search request.", type_hint="dict[str, Any]"),
        required_field("seed_write", "Feishu seed write configuration.", type_hint="dict[str, Any]"),
        optional_field("target_table_ref", "Target TK competitor table reference.", type_hint="str"),
    ),
    result_contract=contract(
        "keyword_seed_import_result",
        required_field("normalized_candidates", "Normalized FastMoss product candidates.", type_hint="list[dict[str, Any]]"),
        required_field("seed_contexts", "Seed write result contexts for downstream row refresh.", type_hint="list[dict[str, Any]]"),
        optional_field("search_summary", "FastMoss search summary.", type_hint="dict[str, Any]"),
        optional_field("seed_write_results", "Per-candidate seed write results.", type_hint="list[dict[str, Any]]"),
    ),
    business_key_template="{search_query}",
    dedupe_key_template="{request_id}:{job_code}:{search_digest}",
    side_effects=("fastmoss", "feishu.write", "runtime_db"),
)


JOB_DEFINITION = KEYWORD_SEED_IMPORT_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = ["HANDLER_CODE", "JOB_CODE", "JOB_DEFINITION", "KEYWORD_SEED_IMPORT_JOB"]
