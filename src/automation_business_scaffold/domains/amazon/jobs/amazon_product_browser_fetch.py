from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)


AMAZON_PRODUCT_BROWSER_FETCH_JOB = JobDefinition(
    job_code="amazon_product_browser_fetch",
    handler_code="amazon_product_browser_fetch",
    worker_type="browser_worker",
    runtime_table="task_execution",
    purpose="Collect one Amazon US product page and persist compact capture references.",
    payload_contract=contract(
        "amazon_product_browser_fetch_payload",
        required_field("requested_asin", "Normalized Amazon US ASIN.", type_hint="str"),
        required_field("source_record_id", "Source Feishu record id.", type_hint="str"),
        required_field("run_id", "Stable collection run id.", type_hint="str"),
    ),
    result_contract=contract(
        "amazon_product_browser_fetch_result",
        required_field("marketplace_code", "Amazon marketplace code.", type_hint="str"),
        required_field("requested_asin", "Requested source ASIN.", type_hint="str"),
        required_field("resolved_asin", "Resolved page ASIN.", type_hint="str"),
        required_field("canonical_url", "Canonical requested product URL.", type_hint="str"),
        required_field("collection_status", "Amazon row collection status.", type_hint="str"),
        required_field("field_coverage", "Compact field coverage counts.", type_hint="dict[str, Any]"),
        required_field(
            "normalized_capture_ref",
            "Object-storage reference for the normalized capture.",
            type_hint="dict[str, Any]",
        ),
        required_field(
            "raw_capture_refs",
            "Compact governed raw evidence references.",
            type_hint="list[dict[str, Any]]",
        ),
        optional_field(
            "artifact_refs",
            "Artifact records indexed by the browser worker.",
            type_hint="list[dict[str, Any]]",
        ),
        optional_field(
            "media_source_refs",
            "Observed media URLs awaiting materialization.",
            type_hint="list[dict[str, Any]]",
        ),
        optional_field(
            "browser_target_digest",
            "Non-sensitive digest of the resolved browser target.",
            type_hint="str",
        ),
    ),
    business_key_template="{source_record_id}:{requested_asin}",
    dedupe_key_template=(
        "{request_id}:amazon_collect:{source_record_id}:{requested_asin}"
    ),
    side_effects=("browser", "object_store", "runtime_db"),
)


JOB_DEFINITION = AMAZON_PRODUCT_BROWSER_FETCH_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = [
    "AMAZON_PRODUCT_BROWSER_FETCH_JOB",
    "HANDLER_CODE",
    "JOB_CODE",
    "JOB_DEFINITION",
]
