from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)

FASTMOSS_SECURITY_BROWSER_RESOLVE_JOB = JobDefinition(
    job_code="fastmoss_security_browser_resolve",
    handler_code="fastmoss_security_browser_resolve",
    worker_type="browser_worker",
    runtime_table="task_execution",
    purpose="Resolve FastMoss search security verification in browser and refresh cookie cache.",
    payload_contract=contract(
        "fastmoss_security_browser_resolve_payload",
        required_field("search_request", "Original FastMoss search request context.", type_hint="dict[str, Any]"),
        optional_field("resource_code", "Browser resource or profile affinity key.", type_hint="str"),
        optional_field("fastmoss", "FastMoss login and browser settings.", type_hint="dict[str, Any]"),
    ),
    result_contract=contract(
        "fastmoss_security_browser_resolve_result",
        required_field("verified_path", "Verified FastMoss API path.", type_hint="str"),
        required_field("cookie_cache", "Redacted FastMoss cookie cache metadata.", type_hint="dict[str, Any]"),
        optional_field("slider_resolution", "Redacted slider handling evidence.", type_hint="dict[str, Any]"),
    ),
    business_key_template="{search_query}",
    dedupe_key_template="{request_id}:{job_code}:{search_digest}:fastmoss_security_browser_fallback",
    side_effects=("browser", "fastmoss.cookie_cache", "runtime_db"),
)


JOB_DEFINITION = FASTMOSS_SECURITY_BROWSER_RESOLVE_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = [
    "FASTMOSS_SECURITY_BROWSER_RESOLVE_JOB",
    "HANDLER_CODE",
    "JOB_CODE",
    "JOB_DEFINITION",
]
