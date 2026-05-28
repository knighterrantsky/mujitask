from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.control_plane.runtime_config.settings import (
    INFLUENCER_OUTREACH_TASK_CODE,
)
from automation_business_scaffold.contracts.workflow import (
    StageDefinition,
    StageJobBinding,
    SummaryStatusRule,
    TransitionDefinition,
    WorkflowDefinition,
    build_formal_task_workflow,
    contract,
    optional_field,
    required_field,
)
from automation_business_scaffold.domains.tiktok.jobs import (
    FASTMOSS_SECURITY_BROWSER_RESOLVE_JOB,
    FEISHU_TABLE_READ_JOB,
    OUTREACH_CREATOR_VIDEO_METRIC_REFRESH_JOB,
    PRODUCT_VIDEO_OUTREACH_CHECK_JOB,
    TASK_COMPLETED_NOTIFICATION_JOB,
)
from automation_business_scaffold.domains.tiktok.policies import (
    DEFAULT_CONTRACT_REVISION,
    STANDARD_ERROR_CONTRACT,
    STANDARD_SUMMARY_CONTRACT,
    influencer_idempotency_rules,
    influencer_timeout_rules,
    notification_summary_policy,
    standard_watchdog_rules,
)


WORKFLOW_CODE = INFLUENCER_OUTREACH_TASK_CODE


def build_tiktok_influencer_outreach_sync_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        task_code=INFLUENCER_OUTREACH_TASK_CODE,
        workflow_code=WORKFLOW_CODE,
        contract_revision=DEFAULT_CONTRACT_REVISION,
        trigger_modes=("manual", "schedule", "cli"),
        entry_stage_code="read_outreach_rows",
        payload_contract=contract(
            "tiktok_influencer_outreach_sync_payload",
            required_field("source_table_ref", "TK influencer outreach table reference.", type_hint="str"),
            optional_field("source_record_ids", "Optional subset of outreach rows to process.", type_hint="list[str]"),
            optional_field("trigger_date", "Task trigger date used as 检查时间.", type_hint="str"),
            optional_field("reply_target", "Reply target used by the final outbox.", type_hint="str"),
            optional_field("writeback_enabled", "Optional Feishu writeback switch; defaults to true and explicit false disables row updates.", type_hint="bool"),
        ),
        stages=(
            StageDefinition(
                stage_code="read_outreach_rows",
                description="Read TK outreach rows and normalize candidates grouped by SKUID.",
                execution_mode="worker_jobs",
                enter_condition="task_request has outreach source table context",
                exit_condition="candidate outreach rows are available or all rows were skipped",
                job_bindings=(
                    StageJobBinding(
                        job_code="feishu_table_read",
                        adapter_code="outreach_source_adapter",
                        result_consumer="product video check fan-out",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="index_product_videos",
                description="Collect FastMoss product video lists and persist product-video indexes.",
                execution_mode="worker_jobs",
                enter_condition="candidate rows have been grouped by SKUID",
                exit_condition="all product video index jobs are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="product_video_outreach_check",
                        flow_code="product_video_outreach_check_flow",
                        result_consumer="creator video metric refresh fan-out",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="fastmoss_security_browser_fallback",
                description="Resolve FastMoss auth or security recovery in browser and refresh shared cookies.",
                execution_mode="worker_jobs",
                enter_condition="product video check returned fallback_required for FastMoss auth/security",
                exit_condition="FastMoss browser recovery is terminal and waiting API jobs are requeued once",
                job_bindings=(
                    StageJobBinding(
                        job_code="fastmoss_security_browser_resolve",
                        flow_code="fastmoss_security_browser_resolve",
                        result_consumer="cookie cache metadata and original FastMoss request verification evidence",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="refresh_creator_video_metrics_and_writeback",
                description="Refresh all known video overview metrics for each SKU creator row and write the Feishu row.",
                execution_mode="worker_jobs",
                enter_condition="product video index jobs are terminal and creator rows are available",
                exit_condition="all creator video metric refresh jobs are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="outreach_creator_video_metric_refresh",
                        flow_code="outreach_creator_video_metric_refresh_flow",
                        result_consumer="row-level outreach metric and writeback result",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="ready_for_summary",
                description="Aggregate outreach check and writeback outcomes and enqueue final notification.",
                execution_mode="summary",
                enter_condition="all outreach checks and writebacks are terminal",
                exit_condition="summary and outbox payload have been persisted",
                job_bindings=(
                    StageJobBinding(
                        job_code="task_completed_notification",
                        flow_code="summary_renderer",
                        result_consumer="user or Feishu final notification",
                    ),
                ),
            ),
        ),
        job_defs=(
            FEISHU_TABLE_READ_JOB,
            PRODUCT_VIDEO_OUTREACH_CHECK_JOB,
            OUTREACH_CREATOR_VIDEO_METRIC_REFRESH_JOB,
            FASTMOSS_SECURITY_BROWSER_RESOLVE_JOB,
            TASK_COMPLETED_NOTIFICATION_JOB,
        ),
        transitions=(
            TransitionDefinition(
                from_stage_code="read_outreach_rows",
                to_stage_code="index_product_videos",
                condition="outreach rows were read and product video index jobs can be created",
            ),
            TransitionDefinition(
                from_stage_code="index_product_videos",
                to_stage_code="refresh_creator_video_metrics_and_writeback",
                condition="product video index jobs are terminal and creator metric refresh jobs can be created",
            ),
            TransitionDefinition(
                from_stage_code="refresh_creator_video_metrics_and_writeback",
                to_stage_code="ready_for_summary",
                condition="creator video metric refresh jobs are terminal",
            ),
        ),
        summary_policy=notification_summary_policy(
            SummaryStatusRule(final_status="success", when="all product indexes and creator metric refresh jobs succeeded"),
            SummaryStatusRule(final_status="partial_success", when="some product indexes or creator metric refresh jobs failed after retries"),
            SummaryStatusRule(final_status="failed", when="outreach rows could not be read or no required side effect completed"),
            notes=("Outbox title defaults to 达人建联检查完成 and must not include FastMoss raw responses or cookies.",),
        ),
        idempotency_policy=influencer_idempotency_rules(),
        timeout_policy=influencer_timeout_rules(),
        watchdog_policy=standard_watchdog_rules(include_browser=True),
        summary_contract=STANDARD_SUMMARY_CONTRACT,
        error_contract=STANDARD_ERROR_CONTRACT,
        notes=("Normal product video matching uses FastMoss HTTP API; browser is only for auth/security recovery.",),
    )


TIKTOK_INFLUENCER_OUTREACH_SYNC_DEFINITION = build_tiktok_influencer_outreach_sync_definition()


def build_tiktok_influencer_outreach_sync_workflow(
    *,
    run_mode: str = "draft",
    control_action: str = "submit",
) -> WorkflowSpec:
    del control_action
    return build_formal_task_workflow(
        workflow_code=WORKFLOW_CODE,
        run_mode=run_mode,
    )
