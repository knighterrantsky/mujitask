from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)


AMAZON_PRODUCT_FACT_UPSERT_JOB = JobDefinition(
    job_code="amazon_product_fact_upsert",
    handler_code="amazon_product_fact_upsert",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Persist one normalized Amazon US capture into the isolated Amazon Fact tables.",
    payload_contract=contract(
        "amazon_product_fact_upsert_payload",
        required_field(
            "normalized_capture_ref",
            "Object-storage reference for the normalized capture JSON.",
            type_hint="dict[str, Any]",
        ),
        required_field(
            "raw_capture_refs",
            "Object-storage references for normalized JSON and sanitized HTML evidence.",
            type_hint="list[dict[str, Any]]",
        ),
        required_field(
            "source_table_ref",
            "Source Feishu base and table identity.",
            type_hint="dict[str, str]",
        ),
        required_field(
            "source_record_id",
            "Source Feishu record id.",
            type_hint="str",
        ),
        required_field("run_id", "Stable collection run id.", type_hint="str"),
        optional_field(
            "materialized_media_assets",
            "Product media already materialized to object storage.",
            type_hint="list[dict[str, Any]]",
        ),
        optional_field(
            "request_payload",
            "Internal runtime configuration handoff; formal task inputs remain business-only.",
            type_hint="dict[str, Any]",
        ),
    ),
    result_contract=contract(
        "amazon_product_fact_upsert_result",
        required_field("product_id", "Amazon product master identifier.", type_hint="str"),
        required_field("snapshot_id", "Immutable collection snapshot identifier.", type_hint="str"),
        required_field("binding_id", "Feishu source binding identifier.", type_hint="str"),
        required_field(
            "normalized_capture_ref",
            "Compact normalized-capture object reference.",
            type_hint="dict[str, Any]",
        ),
        required_field(
            "persisted_counts",
            "Counts of independently persisted Amazon fact rows.",
            type_hint="dict[str, int]",
        ),
        optional_field(
            "raw_capture_ids",
            "Fact identifiers for indexed raw capture evidence.",
            type_hint="list[str]",
        ),
    ),
    business_key_template="{source_record_id}",
    dedupe_key_template="{request_id}:{stage_code}:{source_record_id}:{run_id}",
    side_effects=("artifact.read", "fact_db.write"),
)


JOB_DEFINITION = AMAZON_PRODUCT_FACT_UPSERT_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = [
    "AMAZON_PRODUCT_FACT_UPSERT_JOB",
    "HANDLER_CODE",
    "JOB_CODE",
    "JOB_DEFINITION",
]
