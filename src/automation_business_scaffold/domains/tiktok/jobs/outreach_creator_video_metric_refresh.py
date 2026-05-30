from __future__ import annotations

from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.contracts.workflow import JobDefinition, contract, optional_field, required_field

OUTREACH_CREATOR_VIDEO_METRIC_REFRESH_JOB = JobDefinition(
    job_code="outreach_creator_video_metric_refresh",
    handler_code="outreach_creator_video_metric_refresh",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Refresh all known video overview metrics for one outreach SKU creator row and write the Feishu row.",
    payload_contract=contract(
        "outreach_creator_video_metric_refresh_payload",
        required_field("product_id", "FastMoss product_id / SKUID.", type_hint="str"),
        required_field("creator_unique_id", "TikTok/FastMoss creator unique_id from 达人ID.", type_hint="str"),
        required_field("source_record_id", "Feishu source record id.", type_hint="str"),
        required_field("trigger_date", "Task trigger date used as check/update time.", type_hint="str"),
        optional_field("source_fields", "Original Feishu fields for diff writeback.", type_hint="dict[str, Any]"),
    ),
    result_contract=contract(
        "outreach_creator_video_metric_refresh_result",
        required_field("product_id", "FastMoss product_id / SKUID.", type_hint="str"),
        required_field("creator_unique_id", "TikTok/FastMoss creator unique_id from 达人ID.", type_hint="str"),
        required_field("source_record_id", "Feishu source record id.", type_hint="str"),
        required_field("refresh_status", "success, skipped, or failed.", type_hint="str"),
        optional_field("video_count", "Known videos under this SKU + creator.", type_hint="int"),
        optional_field("total_play_count", "Sum of latest overview play_count for known videos.", type_hint="int"),
        optional_field("highest_play_video_url", "Highest-play video URL.", type_hint="str"),
        optional_field("written_fields", "Feishu fields written by this job.", type_hint="list[str]"),
    ),
    business_key_template="outreach:{product_id}:{creator_unique_id}:{source_record_id}",
    dedupe_key_template="{request_id}:{stage_code}:{product_id}:{creator_unique_id}:{source_record_id}",
    side_effects=("fastmoss.request", "fact_db.write", "feishu.write"),
)

JOB_DEFINITION = OUTREACH_CREATOR_VIDEO_METRIC_REFRESH_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


def outreach_creator_video_metric_refresh_handler(context: HandlerContext) -> HandlerResult:
    from automation_business_scaffold.domains.tiktok.flows.outreach_creator_video_metrics import (
        outreach_creator_video_metric_refresh_handler as _handler,
    )

    result = _handler(context)
    if result.handler_code != HANDLER_CODE:
        raise AssertionError(f"outreach_creator_video_metric_refresh returned handler_code {result.handler_code!r}.")
    return result


__all__ = [
    "CONTRACT",
    "HANDLER_CODE",
    "JOB_CODE",
    "JOB_DEFINITION",
    "OUTREACH_CREATOR_VIDEO_METRIC_REFRESH_JOB",
    "outreach_creator_video_metric_refresh_handler",
]
