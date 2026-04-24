from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)

MEDIA_ASSET_SYNC_JOB = JobDefinition(
    job_code="media_asset_sync",
    handler_code="media_asset_sync",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Sync normalized media assets into object storage and emit media fact references.",
    payload_contract=contract(
        "media_asset_sync_payload",
        required_field("asset_refs", "Media assets to sync.", type_hint="list[dict[str, Any]]"),
        required_field("entity_keys", "Business entity keys that own the assets.", type_hint="list[str]"),
        optional_field("source_context", "Source task or stage context.", type_hint="dict[str, Any]"),
    ),
    result_contract=contract(
        "media_asset_sync_result",
        required_field("synced_assets", "Synced asset records and object refs.", type_hint="list[dict[str, Any]]"),
        optional_field("artifact_refs", "Object store or raw artifact refs.", type_hint="list[str]"),
    ),
    business_key_template="{entity_key}:{asset_source}",
    dedupe_key_template="{request_id}:{job_code}:{entity_key}:{asset_source}",
    side_effects=("object_store", "runtime_db"),
)


JOB_DEFINITION = MEDIA_ASSET_SYNC_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = ["HANDLER_CODE", "JOB_CODE", "JOB_DEFINITION", "MEDIA_ASSET_SYNC_JOB"]
