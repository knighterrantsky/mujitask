from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)

TIKTOK_PRODUCT_BROWSER_FETCH_JOB = JobDefinition(
    job_code="tiktok_product_browser_fetch",
    handler_code="tiktok_product_browser_fetch",
    worker_type="browser_worker",
    runtime_table="task_execution",
    purpose="Collect TikTok product page data through the browser as a fallback path.",
    payload_contract=contract(
        "tiktok_product_browser_fetch_payload",
        required_field("product_identity", "Normalized TikTok product key or URL bundle.", type_hint="dict[str, Any]"),
        required_field("fallback_source_job_id", "Request/API job that triggered the fallback.", type_hint="str"),
        optional_field("resource_code", "Browser resource or profile affinity key.", type_hint="str"),
        optional_field("normalized_product_url", "Canonical product URL for resource and artifact naming.", type_hint="str"),
    ),
    result_contract=contract(
        "tiktok_product_browser_fetch_result",
        required_field("normalized_product_result", "Normalized TikTok product result contract.", type_hint="dict[str, Any]"),
        optional_field("artifact_refs", "Browser artifacts stored for audit or parsing.", type_hint="list[str]"),
        optional_field("fallback_source_job_id", "Original request/API job id.", type_hint="str"),
    ),
    business_key_template="{normalized_product_url}",
    dedupe_key_template="{request_id}:{job_code}:{normalized_product_url}",
    side_effects=("browser", "object_store", "runtime_db"),
)


JOB_DEFINITION = TIKTOK_PRODUCT_BROWSER_FETCH_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = ["HANDLER_CODE", "JOB_CODE", "JOB_DEFINITION", "TIKTOK_PRODUCT_BROWSER_FETCH_JOB"]
