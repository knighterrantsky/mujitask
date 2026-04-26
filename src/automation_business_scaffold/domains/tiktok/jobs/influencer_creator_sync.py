from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)
from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult

INFLUENCER_CREATOR_SYNC_JOB = JobDefinition(
    job_code="influencer_creator_sync",
    handler_code="influencer_creator_sync",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Sync one unique creator into TK influencer pool and reconcile touched product statuses.",
    payload_contract=contract(
        "influencer_creator_sync_payload",
        required_field("creator_identity", "Normalized creator business key.", type_hint="dict[str, Any]"),
        required_field("product_hits", "Products hit by this creator in the current request.", type_hint="list[dict[str, Any]]"),
        optional_field("sync_plan", "Internal capability handler plan.", type_hint="dict[str, Any]"),
    ),
    result_contract=contract(
        "influencer_creator_sync_result",
        required_field("creator_id", "Stable creator id.", type_hint="str"),
        required_field("status", "Creator sync status.", type_hint="str"),
        optional_field("internal_steps", "Internal capability step statuses.", type_hint="dict[str, str]"),
        optional_field("influencer_pool_write", "Feishu influencer pool write payload/result.", type_hint="dict[str, Any]"),
        optional_field("product_status_writebacks", "Product status writebacks completed by this job.", type_hint="list[dict[str, Any]]"),
    ),
    business_key_template="{creator_id}",
    dedupe_key_template="{request_id}:{stage_code}:{creator_id}",
    side_effects=("fastmoss", "fact_db", "artifact", "feishu"),
)


JOB_DEFINITION = INFLUENCER_CREATOR_SYNC_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


def influencer_creator_sync_handler(context: HandlerContext) -> HandlerResult:
    from automation_business_scaffold.domains.tiktok.flows.influencer_sync import (
        run_influencer_creator_sync_flow,
    )

    result = run_influencer_creator_sync_flow(context)
    if result.handler_code != HANDLER_CODE:
        raise AssertionError(
            f"influencer_creator_sync returned handler_code {result.handler_code!r}."
        )
    return result


__all__ = [
    "CONTRACT",
    "HANDLER_CODE",
    "JOB_CODE",
    "JOB_DEFINITION",
    "INFLUENCER_CREATOR_SYNC_JOB",
    "influencer_creator_sync_handler",
]
