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


PRODUCT_VIDEO_CREATOR_DISCOVERY_JOB = JobDefinition(
    job_code="product_video_creator_discovery",
    handler_code="product_video_creator_discovery",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Discover qualifying creators from all FastMoss product videos for one product.",
    payload_contract=contract(
        "product_video_creator_discovery_payload",
        required_field("product_id", "FastMoss product_id / SKU-ID.", type_hint="str"),
        required_field(
            "min_video_sales_28d",
            "Strict lower bound for video-product sales in the 28-day window.",
            type_hint="int",
        ),
        optional_field(
            "source_product_images",
            "Bounded source Feishu attachment references.",
            type_hint="list[dict[str, Any]]",
        ),
        optional_field("holidays", "Source holiday values.", type_hint="list[str]"),
    ),
    result_contract=contract(
        "product_video_creator_discovery_result",
        required_field("product_id", "FastMoss product_id / SKU-ID.", type_hint="str"),
        required_field("fetch_status", "success, empty, or failed.", type_hint="str"),
        required_field(
            "candidates",
            "One maximum qualifying candidate per creator.",
            type_hint="list[dict[str, Any]]",
        ),
        optional_field("pagination", "Compact pagination evidence.", type_hint="dict[str, Any]"),
    ),
    business_key_template="product:{product_id}",
    dedupe_key_template="{request_id}:{stage_code}:{product_id}:{min_video_sales_28d}",
    side_effects=("fastmoss.request", "fact_db.write", "runtime_db"),
)

JOB_DEFINITION = PRODUCT_VIDEO_CREATOR_DISCOVERY_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


def product_video_creator_discovery_handler(
    context: HandlerContext,
) -> HandlerResult:
    from automation_business_scaffold.domains.tiktok.flows.monitor_tk_influencers.product_discovery import (
        product_video_creator_discovery_handler as _handler,
    )

    result = _handler(context)
    if result.handler_code != HANDLER_CODE:
        raise AssertionError(
            f"product_video_creator_discovery returned {result.handler_code!r}."
        )
    return result


__all__ = [
    "CONTRACT",
    "HANDLER_CODE",
    "JOB_CODE",
    "JOB_DEFINITION",
    "PRODUCT_VIDEO_CREATOR_DISCOVERY_JOB",
    "product_video_creator_discovery_handler",
]
