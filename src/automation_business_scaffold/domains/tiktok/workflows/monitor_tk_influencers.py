from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.contracts.workflow import (
    StageDefinition,
    StageJobBinding,
    SummaryStatusRule,
    TransitionDefinition,
    WorkflowDefinition,
    build_formal_task_workflow,
    contract,
    optional_field,
)
from automation_business_scaffold.control_plane.runtime_config.settings import (
    INFLUENCER_MONITOR_TASK_CODE,
)
from automation_business_scaffold.domains.tiktok.jobs import (
    FASTMOSS_SECURITY_BROWSER_RESOLVE_JOB,
    FEISHU_TABLE_READ_JOB,
    INFLUENCER_MONITOR_SYNC_JOB,
    PRODUCT_VIDEO_CREATOR_DISCOVERY_JOB,
    TASK_COMPLETED_NOTIFICATION_JOB,
)
from automation_business_scaffold.domains.tiktok.policies import (
    STANDARD_ERROR_CONTRACT,
    STANDARD_SUMMARY_CONTRACT,
    influencer_idempotency_rules,
    influencer_timeout_rules,
    notification_summary_policy,
    standard_watchdog_rules,
)


WORKFLOW_CODE = INFLUENCER_MONITOR_TASK_CODE


def build_monitor_tk_influencers_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        task_code=INFLUENCER_MONITOR_TASK_CODE,
        workflow_code=WORKFLOW_CODE,
        contract_revision="2",
        trigger_modes=("schedule", "manual", "cli"),
        entry_stage_code="read_competitor_products",
        payload_contract=contract(
            "monitor_tk_influencers_payload",
            optional_field(
                "min_video_sales_28d",
                "Strict minimum FastMoss video-product sales in the 28-day window; default 50.",
                type_hint="int",
            ),
            optional_field(
                "related_product_sales_reset_days",
                "Positive per-creator target sales peak reset period in Shanghai natural days; default 28.",
                type_hint="int",
            ),
            optional_field(
                "task_business_date",
                "Internal immutable Asia/Shanghai task creation date shared by all jobs and retries.",
                type_hint="str",
            ),
            optional_field(
                "source_table_ref",
                "Internal logical TK competitor table reference.",
                type_hint="str",
            ),
            optional_field(
                "target_table_ref",
                "Internal logical TK influencer monitoring target reference.",
                type_hint="str",
            ),
            optional_field(
                "reply_target",
                "Optional final notification reply target.",
                type_hint="str",
            ),
        ),
        stages=(
            StageDefinition(
                stage_code="read_competitor_products",
                description="Read every parseable competitor SKU without product/status filters.",
                execution_mode="worker_jobs",
                enter_condition="task request has logical source table context",
                exit_condition="all source rows are normalized and duplicate products are merged",
                job_bindings=(
                    StageJobBinding(
                        job_code="feishu_table_read",
                        adapter_code="influencer_monitor_source_adapter",
                        result_consumer="product-video creator discovery fan-out",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="discover_product_video_creators",
                description="Query all product videos and keep creator maxima above the configured threshold.",
                execution_mode="worker_jobs",
                enter_condition="deduplicated product candidates are available",
                exit_condition="all product discovery jobs are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="product_video_creator_discovery",
                        flow_code="product_video_creator_discovery",
                        result_consumer="unique creator aggregation",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="fastmoss_security_browser_fallback",
                description="Resolve one FastMoss auth/security recovery attempt for waiting jobs.",
                execution_mode="worker_jobs",
                enter_condition="product discovery or creator sync returned fallback_required",
                exit_condition="waiting jobs are requeued once or marked failed",
                job_bindings=(
                    StageJobBinding(
                        job_code="fastmoss_security_browser_resolve",
                        flow_code="fastmoss_security_browser_resolve",
                        result_consumer="redacted cookie-cache recovery evidence",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="sync_monitored_influencers",
                description="Collect and upsert one independent monitoring target row per creator.",
                execution_mode="worker_jobs",
                enter_condition="unique creator candidates are aggregated across products",
                exit_condition="all creator sync jobs are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="influencer_monitor_sync",
                        flow_code="influencer_monitor_sync",
                        mapper_code="influencer_monitor_projection_mapper",
                        result_consumer="creator write result",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="ready_for_summary",
                description="Aggregate product and creator results and enqueue the final notification.",
                execution_mode="summary",
                enter_condition="all product and creator jobs are terminal",
                exit_condition="summary and outbox are persisted",
                job_bindings=(
                    StageJobBinding(
                        job_code="task_completed_notification",
                        flow_code="summary_renderer",
                        result_consumer="final notification",
                    ),
                ),
            ),
        ),
        job_defs=(
            FEISHU_TABLE_READ_JOB,
            PRODUCT_VIDEO_CREATOR_DISCOVERY_JOB,
            FASTMOSS_SECURITY_BROWSER_RESOLVE_JOB,
            INFLUENCER_MONITOR_SYNC_JOB,
            TASK_COMPLETED_NOTIFICATION_JOB,
        ),
        transitions=(
            TransitionDefinition(
                from_stage_code="read_competitor_products",
                to_stage_code="discover_product_video_creators",
                condition="source read is terminal",
            ),
            TransitionDefinition(
                from_stage_code="discover_product_video_creators",
                to_stage_code="fastmoss_security_browser_fallback",
                condition="a product discovery job requires FastMoss security recovery",
            ),
            TransitionDefinition(
                from_stage_code="fastmoss_security_browser_fallback",
                to_stage_code="discover_product_video_creators",
                condition="a product discovery job was requeued",
            ),
            TransitionDefinition(
                from_stage_code="discover_product_video_creators",
                to_stage_code="sync_monitored_influencers",
                condition="product jobs are terminal and unique creators can be dispatched",
            ),
            TransitionDefinition(
                from_stage_code="sync_monitored_influencers",
                to_stage_code="fastmoss_security_browser_fallback",
                condition="a creator sync job requires FastMoss security recovery",
            ),
            TransitionDefinition(
                from_stage_code="fastmoss_security_browser_fallback",
                to_stage_code="sync_monitored_influencers",
                condition="a creator sync job was requeued",
            ),
            TransitionDefinition(
                from_stage_code="sync_monitored_influencers",
                to_stage_code="ready_for_summary",
                condition="creator sync jobs are terminal",
            ),
        ),
        summary_policy=notification_summary_policy(
            SummaryStatusRule(
                final_status="success",
                when="source read succeeded and all dispatched product and creator jobs succeeded or were empty",
            ),
            SummaryStatusRule(
                final_status="partial_success",
                when="some product or creator jobs failed while another business item succeeded",
            ),
            SummaryStatusRule(
                final_status="failed",
                when="source read failed or no required side effect completed",
            ),
            notes=("Outbox title defaults to TK达人监控完成.",),
        ),
        idempotency_policy=influencer_idempotency_rules(),
        timeout_policy=influencer_timeout_rules(),
        watchdog_policy=standard_watchdog_rules(include_browser=True),
        summary_contract=STANDARD_SUMMARY_CONTRACT,
        error_contract=STANDARD_ERROR_CONTRACT,
        notes=(
            "This workflow does not import or call another influencer workflow, job, projection, or field contract.",
        ),
    )


MONITOR_TK_INFLUENCERS_DEFINITION = build_monitor_tk_influencers_definition()


def build_monitor_tk_influencers_workflow(
    *,
    run_mode: str = "draft",
    control_action: str = "submit",
) -> WorkflowSpec:
    del control_action
    return build_formal_task_workflow(
        workflow_code=WORKFLOW_CODE,
        run_mode=run_mode,
    )


__all__ = [
    "MONITOR_TK_INFLUENCERS_DEFINITION",
    "WORKFLOW_CODE",
    "build_monitor_tk_influencers_definition",
    "build_monitor_tk_influencers_workflow",
]
