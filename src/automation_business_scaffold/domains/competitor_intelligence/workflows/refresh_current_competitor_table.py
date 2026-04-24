from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.contracts.workflow import build_formal_task_workflow
from automation_business_scaffold.domains.competitor_intelligence.jobs import (
    FACT_BUNDLE_UPSERT_JOB,
    FASTMOSS_PRODUCT_FETCH_JOB,
    FEISHU_TABLE_READ_JOB,
    FEISHU_TABLE_WRITE_JOB,
    MEDIA_ASSET_SYNC_JOB,
    TASK_COMPLETED_NOTIFICATION_JOB,
    TIKTOK_PRODUCT_BROWSER_FETCH_JOB,
    TIKTOK_PRODUCT_REQUEST_FETCH_JOB,
)
from automation_business_scaffold.domains.competitor_intelligence.policies import (
    DEFAULT_CONTRACT_REVISION,
    STANDARD_ERROR_CONTRACT,
    STANDARD_SUMMARY_CONTRACT,
    notification_summary_policy,
    standard_watchdog_rules,
    table_workflow_idempotency_rules,
    table_workflow_timeout_rules,
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


def build_refresh_current_competitor_table_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        task_code="refresh_current_competitor_table",
        workflow_code="refresh_current_competitor_table",
        contract_revision=DEFAULT_CONTRACT_REVISION,
        trigger_modes=("manual", "schedule", "cli"),
        entry_stage_code="read_competitor_rows",
        payload_contract=contract(
            "refresh_current_competitor_table_payload",
            required_field(
                "source_table_ref",
                "Source TK competitor table reference.",
                type_hint="str",
            ),
            optional_field("view_ref", "Optional Feishu view identifier.", type_hint="str"),
            optional_field(
                "source_record_ids",
                "Optional subset of source record ids to refresh.",
                type_hint="list[str]",
            ),
            optional_field(
                "refresh_filter",
                "Normalized filter for pending or retry rows.",
                type_hint="dict[str, Any]",
            ),
            optional_field("reply_target", "Reply target used by the final outbox.", type_hint="str"),
        ),
        stages=(
            StageDefinition(
                stage_code="read_competitor_rows",
                description="Read and normalize candidate competitor rows from TK competitor table.",
                execution_mode="worker_jobs",
                enter_condition="task_request has been claimed and source table context is available",
                exit_condition="source rows have been read and normalized for executor fan-out",
                job_bindings=(
                    StageJobBinding(
                        job_code="feishu_table_read",
                        adapter_code="competitor_table_source_adapter",
                        result_consumer="executor fan-out target rows",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="dispatch_product_collection",
                description="Fan out row-level product collection jobs for TikTok and FastMoss fetch.",
                execution_mode="executor_action",
                enter_condition="candidate competitor rows are available",
                exit_condition="per-row product collection jobs have been created or skipped",
                executor_action_code="fanout_competitor_rows",
                notes=(
                    "This stage remains an executor action in phase one because it is lightweight and idempotent.",
                ),
            ),
            StageDefinition(
                stage_code="collect_product_data",
                description="Run request-first TikTok collection and FastMoss product fetch for each candidate.",
                execution_mode="worker_jobs",
                enter_condition="row-level product collection jobs have been dispatched",
                exit_condition="product collection jobs are terminal or request explicit browser fallback",
                job_bindings=(
                    StageJobBinding(
                        job_code="tiktok_product_request_fetch",
                        flow_code="tiktok_request_flow",
                        result_consumer="fact_bundle_upsert or fallback decision",
                    ),
                    StageJobBinding(
                        job_code="fastmoss_product_fetch",
                        flow_code="fastmoss_product_flow",
                        result_consumer="fact_bundle_upsert",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="browser_fallback",
                description="Execute browser fallback only for product rows that require TikTok browser recovery.",
                execution_mode="worker_jobs",
                enter_condition="at least one TikTok request job returned fallback_required=true",
                exit_condition="browser fallback executions are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="tiktok_product_browser_fetch",
                        flow_code="browser_product_page_flow",
                        result_consumer="normalized product result",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="persist_facts",
                description="Sync media assets and persist normalized facts into Fact DB.",
                execution_mode="worker_jobs",
                enter_condition="normalized product facts exist from request or browser collection",
                exit_condition="media sync and fact upsert jobs are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="media_asset_sync",
                        flow_code="media_object_store_flow",
                        result_consumer="fact_bundle_upsert or competitor projection",
                    ),
                    StageJobBinding(
                        job_code="fact_bundle_upsert",
                        mapper_code="competitor_fact_relation_mapper",
                        result_consumer="competitor writeback projection",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="writeback_competitor_rows",
                description="Project refreshed facts and status back to TK competitor rows.",
                execution_mode="worker_jobs",
                enter_condition="fact upsert has produced competitor-row projection data",
                exit_condition="competitor row writeback jobs are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="feishu_table_write",
                        mapper_code="competitor_table_projection_mapper",
                        result_consumer="row terminal result",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="ready_for_summary",
                description="Aggregate row outcomes and enqueue the final notification payload.",
                execution_mode="summary",
                enter_condition="all child jobs for row refresh are terminal",
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
            TIKTOK_PRODUCT_REQUEST_FETCH_JOB,
            FASTMOSS_PRODUCT_FETCH_JOB,
            TIKTOK_PRODUCT_BROWSER_FETCH_JOB,
            MEDIA_ASSET_SYNC_JOB,
            FACT_BUNDLE_UPSERT_JOB,
            FEISHU_TABLE_WRITE_JOB,
            TASK_COMPLETED_NOTIFICATION_JOB,
        ),
        transitions=(
            TransitionDefinition(
                from_stage_code="read_competitor_rows",
                to_stage_code="dispatch_product_collection",
                condition="feishu_table_read is terminal and source rows have been normalized",
            ),
            TransitionDefinition(
                from_stage_code="dispatch_product_collection",
                to_stage_code="collect_product_data",
                condition="row fan-out has completed or no rows require product collection",
            ),
            TransitionDefinition(
                from_stage_code="collect_product_data",
                to_stage_code="browser_fallback",
                condition="at least one tiktok_product_request_fetch result requires browser fallback",
                transition_type="conditional",
            ),
            TransitionDefinition(
                from_stage_code="collect_product_data",
                to_stage_code="persist_facts",
                condition="product collection jobs are terminal and no browser fallback is pending",
                transition_type="conditional",
            ),
            TransitionDefinition(
                from_stage_code="browser_fallback",
                to_stage_code="persist_facts",
                condition="browser fallback executions are terminal",
            ),
            TransitionDefinition(
                from_stage_code="persist_facts",
                to_stage_code="writeback_competitor_rows",
                condition="media sync and fact upsert jobs are terminal",
            ),
            TransitionDefinition(
                from_stage_code="writeback_competitor_rows",
                to_stage_code="ready_for_summary",
                condition="competitor writeback jobs are terminal",
            ),
        ),
        summary_policy=notification_summary_policy(
            SummaryStatusRule(
                final_status="success",
                when="all dispatched collection, fact, and writeback jobs completed without failed rows",
            ),
            SummaryStatusRule(
                final_status="partial_success",
                when="at least one competitor row refreshed successfully but some child jobs failed or were skipped",
            ),
            SummaryStatusRule(
                final_status="failed",
                when="no competitor row produced a persisted result or orchestration failed irrecoverably",
            ),
            notes=(
                "Summary should preserve per-row success, skipped, and failed counts for operator review.",
            ),
        ),
        idempotency_policy=table_workflow_idempotency_rules(
            request_scope="{request_id}:{workflow_code}",
            row_scope="{request_id}:{stage_code}:{source_record_id_or_product_id}",
        ),
        timeout_policy=table_workflow_timeout_rules(include_browser=True),
        watchdog_policy=standard_watchdog_rules(include_browser=True),
        summary_contract=STANDARD_SUMMARY_CONTRACT,
        error_contract=STANDARD_ERROR_CONTRACT,
        notes=(
            "Task submission itself is handled by task_request creation; the internal entry stage starts at read_competitor_rows.",
            "This definition follows the cross-workflow stage mapping in workflow-redesign-review.md.",
        ),
    )


REFRESH_CURRENT_COMPETITOR_TABLE_DEFINITION = build_refresh_current_competitor_table_definition()


def build_refresh_current_competitor_table_workflow(
    *,
    run_mode: str = "draft",
    control_action: str = "submit",
) -> WorkflowSpec:
    del control_action
    return build_formal_task_workflow(
        workflow_code="refresh_current_competitor_table",
        run_mode=run_mode,
    )
