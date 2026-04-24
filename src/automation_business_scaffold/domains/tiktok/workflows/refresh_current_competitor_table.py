from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.contracts.workflow import build_formal_task_workflow
from automation_business_scaffold.domains.tiktok.jobs import (
    COMPETITOR_ROW_REFRESH_JOB,
    FEISHU_TABLE_READ_JOB,
    FEISHU_TABLE_WRITE_JOB,
    TASK_COMPLETED_NOTIFICATION_JOB,
)
from automation_business_scaffold.domains.tiktok.policies import (
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
                description="Fan out one row-level pipeline job for each candidate competitor row.",
                execution_mode="executor_action",
                enter_condition="candidate competitor rows are available",
                exit_condition="per-row competitor pipeline jobs have been created or skipped",
                executor_action_code="fanout_competitor_rows",
                notes=(
                    "This stage remains an executor action because it is lightweight and idempotent.",
                ),
            ),
            StageDefinition(
                stage_code="collect_product_data",
                description="Run one row-level competitor refresh pipeline job per candidate row.",
                execution_mode="worker_jobs",
                enter_condition="row-level competitor refresh jobs have been dispatched",
                exit_condition="competitor row pipeline jobs are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="competitor_row_refresh",
                        flow_code="competitor_row_pipeline",
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
            FEISHU_TABLE_WRITE_JOB,
            COMPETITOR_ROW_REFRESH_JOB,
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
                condition="row fan-out has completed or no rows require refresh",
            ),
            TransitionDefinition(
                from_stage_code="collect_product_data",
                to_stage_code="ready_for_summary",
                condition="competitor row pipeline jobs are terminal",
            ),
        ),
        summary_policy=notification_summary_policy(
            SummaryStatusRule(
                final_status="success",
                when="all dispatched competitor row pipeline jobs completed without failed rows",
            ),
            SummaryStatusRule(
                final_status="partial_success",
                when="at least one competitor row refreshed successfully but some row pipelines were partial or failed",
            ),
            SummaryStatusRule(
                final_status="failed",
                when="no competitor row pipeline produced a successful writeback result or orchestration failed irrecoverably",
            ),
            notes=(
                "Summary should preserve per-row success, partial, and failed counts for operator review.",
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
