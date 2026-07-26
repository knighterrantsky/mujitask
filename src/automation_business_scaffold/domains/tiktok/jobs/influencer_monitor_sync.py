from __future__ import annotations

from automation_business_scaffold.contracts.handler.allowlist import (
    API_HANDLER_CONTRACTS,
)
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerResult,
)
from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)


INFLUENCER_MONITOR_SYNC_JOB = JobDefinition(
    job_code="influencer_monitor_sync",
    handler_code="influencer_monitor_sync",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Collect one monitored creator and upsert the independent TK monitoring target.",
    payload_contract=contract(
        "influencer_monitor_sync_payload",
        required_field(
            "creator_identity",
            "Canonical creator identity whose creator_id equals stable uid.",
            type_hint="dict[str, Any]",
        ),
        required_field(
            "creator_run_max_sales_28d",
            "Maximum qualifying video-product sales across the current run.",
            type_hint="int | float",
        ),
        optional_field(
            "product_hits",
            "All qualifying product relationships for field union.",
            type_hint="list[dict[str, Any]]",
        ),
    ),
    result_contract=contract(
        "influencer_monitor_sync_result",
        required_field("creator_id", "Canonical monitored creator id.", type_hint="str"),
        required_field("write_status", "Target table write outcome.", type_hint="str"),
        optional_field("write_summary", "Compact target write counts.", type_hint="dict[str, Any]"),
    ),
    business_key_template="creator:{creator_id}",
    dedupe_key_template="{request_id}:{stage_code}:{creator_id}",
    side_effects=(
        "fastmoss.request",
        "artifact.write",
        "fact_db.write",
        "feishu.write",
        "runtime_db",
    ),
)

JOB_DEFINITION = INFLUENCER_MONITOR_SYNC_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


def influencer_monitor_sync_handler(context: HandlerContext) -> HandlerResult:
    from automation_business_scaffold.domains.tiktok.flows.monitor_tk_influencers.creator_sync import (
        influencer_monitor_sync_handler as _handler,
    )

    result = _handler(context)
    if result.handler_code != HANDLER_CODE:
        raise AssertionError(f"influencer_monitor_sync returned {result.handler_code!r}.")
    return result


__all__ = [
    "CONTRACT",
    "HANDLER_CODE",
    "INFLUENCER_MONITOR_SYNC_JOB",
    "JOB_CODE",
    "JOB_DEFINITION",
    "influencer_monitor_sync_handler",
]
