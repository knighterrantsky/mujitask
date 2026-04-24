from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)

FACT_BUNDLE_UPSERT_JOB = JobDefinition(
    job_code="fact_bundle_upsert",
    handler_code="fact_bundle_upsert",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Upsert normalized entities, relations, observations, and raw links into Fact DB.",
    payload_contract=contract(
        "fact_bundle_upsert_payload",
        required_field("fact_bundle", "Normalized entities, relations, and observations.", type_hint="dict[str, Any]"),
        optional_field("observation_context", "Observation and snapshot context.", type_hint="dict[str, Any]"),
    ),
    result_contract=contract(
        "fact_bundle_upsert_result",
        required_field("upserted_entities", "Entity keys written to Fact DB.", type_hint="list[str]"),
        optional_field("upserted_relations", "Relation keys written to Fact DB.", type_hint="list[str]"),
        optional_field("observation_refs", "Observation refs created during upsert.", type_hint="list[str]"),
    ),
    business_key_template="{entity_business_keys}",
    dedupe_key_template="{request_id}:{job_code}:{entity_business_keys}:{observation_at}",
    side_effects=("fact_db", "runtime_db"),
)


JOB_DEFINITION = FACT_BUNDLE_UPSERT_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = ["HANDLER_CODE", "JOB_CODE", "JOB_DEFINITION", "FACT_BUNDLE_UPSERT_JOB"]
