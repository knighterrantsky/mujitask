from __future__ import annotations

from automation_business_scaffold.contracts.handler.allowlist import BROWSER_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.contracts.workflow import JobDefinition, contract, optional_field, required_field

PRODUCT_VIDEO_OUTREACH_CHECK_JOB = JobDefinition(
    job_code="product_video_outreach_check",
    handler_code="product_video_outreach_check",
    worker_type="browser_worker",
    runtime_table="task_execution",
    purpose="Collect FastMoss product videos in browser for outreach creator matches.",
    payload_contract=contract(
        "product_video_outreach_check_payload",
        required_field("product_id", "FastMoss product_id / SKUID.", type_hint="str"),
        required_field("rows", "Outreach rows for this product.", type_hint="list[dict[str, Any]]"),
        required_field("query_window", "FastMoss video query window.", type_hint="dict[str, Any]"),
        optional_field("trigger_date", "Task trigger date used as check time.", type_hint="str"),
    ),
    result_contract=contract(
        "product_video_outreach_check_result",
        required_field("product_id", "FastMoss product_id / SKUID.", type_hint="str"),
        required_field("fetch_status", "success or failed.", type_hint="str"),
        optional_field("matched_rows", "Rows with matched videos.", type_hint="list[dict[str, Any]]"),
        optional_field("unmatched_rows", "Rows without matched videos after successful fetch.", type_hint="list[dict[str, Any]]"),
    ),
    business_key_template="product:{product_id}",
    dedupe_key_template="{request_id}:{stage_code}:{product_id}:{query_window}",
    side_effects=("browser", "artifact.write", "runtime_db"),
)

JOB_DEFINITION = PRODUCT_VIDEO_OUTREACH_CHECK_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code
CONTRACT = BROWSER_HANDLER_CONTRACTS[HANDLER_CODE]


def product_video_outreach_check_handler(context: HandlerContext) -> HandlerResult:
    from automation_business_scaffold.capabilities.browser.fastmoss_product_video_outreach_handler import (
        fastmoss_product_video_outreach_handler as _handler,
    )

    result = _handler(context)
    if result.handler_code != HANDLER_CODE:
        raise AssertionError(f"product_video_outreach_check returned handler_code {result.handler_code!r}.")
    return result


__all__ = [
    "CONTRACT",
    "HANDLER_CODE",
    "JOB_CODE",
    "JOB_DEFINITION",
    "PRODUCT_VIDEO_OUTREACH_CHECK_JOB",
    "product_video_outreach_check_handler",
]
