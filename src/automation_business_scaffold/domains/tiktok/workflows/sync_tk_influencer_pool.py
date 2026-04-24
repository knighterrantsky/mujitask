from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.contracts.workflow import build_formal_task_workflow
from automation_business_scaffold.domains.tiktok.jobs import (
    FACT_BUNDLE_UPSERT_JOB,
    FASTMOSS_CREATOR_FETCH_JOB,
    FASTMOSS_PRODUCT_FETCH_JOB,
    FEISHU_TABLE_READ_JOB,
    FEISHU_TABLE_WRITE_JOB,
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
from automation_business_scaffold.contracts.workflow import (
    StageDefinition,
    StageJobBinding,
    SummaryStatusRule,
    TransitionDefinition,
    WorkflowDefinition,
    contract,
    optional_field,
    required_field,
)


def build_sync_tk_influencer_pool_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        task_code="sync_tk_influencer_pool",
        workflow_code="sync_tk_influencer_pool",
        contract_revision=DEFAULT_CONTRACT_REVISION,
        trigger_modes=("manual", "schedule", "cli"),
        entry_stage_code="read_competitor_candidates",
        payload_contract=contract(
            "sync_tk_influencer_pool_payload",
            required_field("source_table_ref", "Source TK competitor table reference.", type_hint="str"),
            optional_field(
                "source_record_ids",
                "Optional subset of competitor rows to process.",
                type_hint="list[str]",
            ),
            optional_field(
                "candidate_filter",
                "Normalized filter for pending or retry product candidates.",
                type_hint="dict[str, Any]",
            ),
            optional_field(
                "reply_target",
                "Reply target used by the final outbox.",
                type_hint="str",
            ),
        ),
        stages=(
            StageDefinition(
                stage_code="read_competitor_candidates",
                description="Read and normalize competitor candidates that require influencer discovery.",
                execution_mode="worker_jobs",
                enter_condition="task_request has source competitor table context",
                exit_condition="candidate product rows are available for product discovery fan-out",
                job_bindings=(
                    StageJobBinding(
                        job_code="feishu_table_read",
                        adapter_code="influencer_pool_source_adapter",
                        result_consumer="product discovery job fan-out",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="dispatch_product_jobs",
                description="Create logical product discovery jobs for influencer lookup.",
                execution_mode="executor_action",
                enter_condition="candidate product rows are available",
                exit_condition="product discovery jobs have been created or skipped",
                executor_action_code="fanout_influencer_products",
            ),
            StageDefinition(
                stage_code="discover_related_creators",
                description="Fetch related creators from FastMoss for each product candidate.",
                execution_mode="worker_jobs",
                enter_condition="product discovery jobs are ready to run",
                exit_condition="creator candidates have been discovered or product discovery is terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="fastmoss_product_fetch",
                        flow_code="fastmoss_product_flow",
                        detail_level="related_creators",
                        result_consumer="creator detail job fan-out",
                        notes=("Handler result should expose creator candidates for reconciler fan-out.",),
                    ),
                ),
            ),
            StageDefinition(
                stage_code="collect_creator_detail",
                description="Fetch creator detail and normalized creator facts for each discovered creator.",
                execution_mode="worker_jobs",
                enter_condition="creator detail jobs have been created",
                exit_condition="creator detail jobs are terminal or waiting for retry",
                job_bindings=(
                    StageJobBinding(
                        job_code="fastmoss_creator_fetch",
                        flow_code="fastmoss_creator_flow",
                        result_consumer="influencer projection payload",
                        notes=("Creator detail may include optional media refs that later mappers can consume.",),
                    ),
                ),
            ),
            StageDefinition(
                stage_code="persist_creator_facts",
                description="Persist product and creator fact bundles before Feishu influencer projection.",
                execution_mode="worker_jobs",
                enter_condition="product or creator fact bundles are available from FastMoss handlers",
                exit_condition="fact upsert jobs are terminal or no facts need persistence",
                job_bindings=(
                    StageJobBinding(
                        job_code="fact_bundle_upsert",
                        result_consumer="fact projection checkpoint before influencer pool write",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="write_influencer_pool",
                description="Project creator detail into TK influencer pool rows.",
                execution_mode="worker_jobs",
                enter_condition="creator facts are available for projection",
                exit_condition="influencer pool writes are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="feishu_table_write",
                        mapper_code="influencer_pool_projection_mapper",
                        result_consumer="creator terminal projection result",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="finalize_product",
                description="Aggregate creator detail outcomes back into product-group terminal state.",
                execution_mode="executor_action",
                enter_condition="creator detail and influencer pool child jobs can be reconciled per product",
                exit_condition="product groups are terminal or scheduled for retry/follow-up",
                executor_action_code="finalize_product",
                notes=(
                    "This is a reconciler-friendly executor action, not a worker job.",
                ),
            ),
            StageDefinition(
                stage_code="writeback_competitor_status",
                description="Write influencer discovery status back to TK competitor rows.",
                execution_mode="worker_jobs",
                enter_condition="product groups are terminal and have competitor-status projection data",
                exit_condition="competitor status writeback jobs are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="feishu_table_write",
                        mapper_code="competitor_influencer_status_projection_mapper",
                        result_consumer="source competitor row status",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="ready_for_summary",
                description="Aggregate product-group and creator outcomes and enqueue the final notification.",
                execution_mode="summary",
                enter_condition="all product groups are terminal",
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
            FASTMOSS_PRODUCT_FETCH_JOB,
            FASTMOSS_CREATOR_FETCH_JOB,
            FACT_BUNDLE_UPSERT_JOB,
            FEISHU_TABLE_WRITE_JOB,
            TASK_COMPLETED_NOTIFICATION_JOB,
        ),
        transitions=(
            TransitionDefinition(
                from_stage_code="read_competitor_candidates",
                to_stage_code="dispatch_product_jobs",
                condition="candidate read job is terminal and product candidates were normalized",
            ),
            TransitionDefinition(
                from_stage_code="dispatch_product_jobs",
                to_stage_code="discover_related_creators",
                condition="product discovery jobs have been created or all candidates were skipped",
            ),
            TransitionDefinition(
                from_stage_code="discover_related_creators",
                to_stage_code="collect_creator_detail",
                condition="creator detail jobs have been created or product discovery groups reached terminal state",
            ),
            TransitionDefinition(
                from_stage_code="collect_creator_detail",
                to_stage_code="persist_creator_facts",
                condition="creator detail jobs are terminal and fact bundles are ready to upsert",
            ),
            TransitionDefinition(
                from_stage_code="persist_creator_facts",
                to_stage_code="write_influencer_pool",
                condition="fact upsert jobs are terminal or no fact bundles were produced",
            ),
            TransitionDefinition(
                from_stage_code="write_influencer_pool",
                to_stage_code="finalize_product",
                condition="influencer pool writes are terminal",
            ),
            TransitionDefinition(
                from_stage_code="finalize_product",
                to_stage_code="writeback_competitor_status",
                condition="product groups are terminal and competitor status projection is ready",
            ),
            TransitionDefinition(
                from_stage_code="writeback_competitor_status",
                to_stage_code="ready_for_summary",
                condition="competitor status writeback jobs are terminal",
            ),
        ),
        summary_policy=notification_summary_policy(
            SummaryStatusRule(
                final_status="success",
                when="all product groups completed and creator detail jobs ended in success or skipped",
            ),
            SummaryStatusRule(
                final_status="partial_success",
                when="at least one influencer projection succeeded but some product or creator jobs failed",
            ),
            SummaryStatusRule(
                final_status="failed",
                when="no influencer projection was written or orchestration failed irrecoverably",
            ),
            notes=(
                "Summary should preserve product-group and creator-level counts for operator review.",
            ),
        ),
        idempotency_policy=influencer_idempotency_rules(),
        timeout_policy=influencer_timeout_rules(),
        watchdog_policy=standard_watchdog_rules(include_browser=False),
        summary_contract=STANDARD_SUMMARY_CONTRACT,
        error_contract=STANDARD_ERROR_CONTRACT,
        notes=(
            "Product discovery and creator detail remain logical api_worker_job families; no workflow-specific runtime table is introduced.",
        ),
    )


SYNC_TK_INFLUENCER_POOL_DEFINITION = build_sync_tk_influencer_pool_definition()


def build_sync_tk_influencer_pool_workflow(
    *,
    run_mode: str = "draft",
    control_action: str = "submit",
) -> WorkflowSpec:
    del control_action
    return build_formal_task_workflow(
        workflow_code="sync_tk_influencer_pool",
        run_mode=run_mode,
    )
