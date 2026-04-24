from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)

TIKTOK_PRODUCT_REQUEST_FETCH_JOB = JobDefinition(
    job_code="tiktok_product_request_fetch",
    handler_code="tiktok_product_request_fetch",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Fetch and normalize TikTok product facts through request/API paths before any browser fallback.",
    payload_contract=contract(
        "tiktok_product_request_fetch_payload",
        required_field("product_identity", "Normalized TikTok product key or URL bundle.", type_hint="dict[str, Any]"),
        optional_field("normalized_product_url", "Canonical product URL for dedupe and artifact naming.", type_hint="str"),
        optional_field("source_context", "Business source row or source request context.", type_hint="dict[str, Any]"),
        optional_field("fallback_allowed", "Whether browser fallback is permitted for this request.", type_hint="bool"),
    ),
    result_contract=contract(
        "tiktok_product_request_fetch_result",
        optional_field("normalized_product_result", "Normalized TikTok product result contract.", type_hint="dict[str, Any]"),
        optional_field("fallback_required", "Whether browser fallback should be dispatched.", type_hint="bool"),
        optional_field("fallback_reason", "Stable fallback reason code.", type_hint="str"),
        optional_field("fallback_source_job_id", "Source API job that requested fallback.", type_hint="str"),
    ),
    business_key_template="{product_id_or_url}",
    dedupe_key_template="{request_id}:{stage_code}:{product_id_or_url}",
    side_effects=("runtime_db",),
    notes=(
        "Handlers should prefer request/API collection and only request browser fallback for recoverable cases.",
    ),
)


JOB_DEFINITION = TIKTOK_PRODUCT_REQUEST_FETCH_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = ["HANDLER_CODE", "JOB_CODE", "JOB_DEFINITION", "TIKTOK_PRODUCT_REQUEST_FETCH_JOB"]
