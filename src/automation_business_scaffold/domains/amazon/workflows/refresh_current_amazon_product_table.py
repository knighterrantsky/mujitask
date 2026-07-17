from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.contracts.workflow import (
    IdempotencyRule,
    StageDefinition,
    StageJobBinding,
    SummaryPolicy,
    SummaryStatusRule,
    TimeoutRule,
    TransitionDefinition,
    WorkflowDefinition,
    build_formal_task_workflow,
    contract,
    required_field,
)
from automation_business_scaffold.domains.amazon.jobs import (
    AMAZON_PRODUCT_BROWSER_FETCH_JOB,
    AMAZON_PRODUCT_ROW_REFRESH_JOB,
    FEISHU_TABLE_READ_JOB,
    TASK_COMPLETED_NOTIFICATION_JOB,
)


WORKFLOW_CODE = "refresh_current_amazon_product_table"


def build_refresh_current_amazon_product_table_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        task_code=WORKFLOW_CODE,
        workflow_code=WORKFLOW_CODE,
        contract_revision="2026-07-16-row-job",
        trigger_modes=("manual", "cli"),
        entry_stage_code="read_amazon_product_rows",
        payload_contract=contract(
            "refresh_current_amazon_product_table_payload",
            required_field("table_ref", "Configured Amazon Feishu table alias.", type_hint="str"),
            notes=("The batch selector is fixed to 采集标签 = T and is not user-configurable.",),
        ),
        stages=(
            StageDefinition(
                stage_code="read_amazon_product_rows",
                description="Read Amazon竞品表 and select rows whose 采集标签 is exactly T.",
                execution_mode="worker_jobs",
                enter_condition="the formal request contains table_ref",
                exit_condition="tagged rows are normalized and invalid ASIN rows are counted",
                job_bindings=(
                    StageJobBinding(
                        job_code="feishu_table_read",
                        adapter_code="amazon_product_batch_source_adapter",
                        result_consumer="validated T-tagged Amazon source rows",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="dispatch_amazon_product_rows",
                description="Create one idempotent Amazon row job per selected record under this request.",
                execution_mode="executor_action",
                enter_condition="the tagged source row list is available",
                exit_condition="every selected record has one same-request row job",
                executor_action_code="dispatch_amazon_product_row_jobs",
            ),
            StageDefinition(
                stage_code="collect_amazon_product_rows",
                description="Run and resume the same-request Amazon row refresh jobs.",
                execution_mode="worker_jobs",
                enter_condition="the row job fan-out is persisted",
                exit_condition="all row jobs are terminal or at least one row requires a browser",
                job_bindings=(
                    StageJobBinding(
                        job_code="amazon_product_row_refresh",
                        flow_code="amazon_product_row_refresh",
                        result_consumer="terminal row outcomes or browser_required handoffs",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="collect_amazon_product_browsers",
                description="Run primary Amazon browser executions and requeue the same waiting row jobs.",
                execution_mode="worker_jobs",
                enter_condition="one or more row jobs returned browser_required",
                exit_condition="browser executions are terminal and their row jobs are requeued",
                job_bindings=(
                    StageJobBinding(
                        job_code="amazon_product_browser_fetch",
                        flow_code="amazon_product_browser_fetch",
                        result_consumer="compact browser capture supplied to the waiting row job",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="ready_for_summary",
                description="Aggregate row-job outcomes and enqueue one batch notification.",
                execution_mode="summary",
                enter_condition="no Amazon row job or browser execution remains active",
                exit_condition="batch summary, result, and outbox are persisted",
                job_bindings=(
                    StageJobBinding(
                        job_code="task_completed_notification",
                        flow_code="summary_renderer",
                        result_consumer="final Amazon batch notification",
                    ),
                ),
            ),
        ),
        job_defs=(
            FEISHU_TABLE_READ_JOB,
            AMAZON_PRODUCT_ROW_REFRESH_JOB,
            AMAZON_PRODUCT_BROWSER_FETCH_JOB,
            TASK_COMPLETED_NOTIFICATION_JOB,
        ),
        transitions=(
            TransitionDefinition(
                from_stage_code="read_amazon_product_rows",
                to_stage_code="dispatch_amazon_product_rows",
                condition="the T-tagged row read is terminal",
            ),
            TransitionDefinition(
                from_stage_code="dispatch_amazon_product_rows",
                to_stage_code="collect_amazon_product_rows",
                condition="row job fan-out is persisted",
            ),
            TransitionDefinition(
                from_stage_code="collect_amazon_product_rows",
                to_stage_code="collect_amazon_product_browsers",
                condition="one or more row jobs require primary browser collection",
            ),
            TransitionDefinition(
                from_stage_code="collect_amazon_product_browsers",
                to_stage_code="collect_amazon_product_rows",
                condition="browser results have requeued their waiting row jobs",
            ),
            TransitionDefinition(
                from_stage_code="collect_amazon_product_rows",
                to_stage_code="ready_for_summary",
                condition="all row jobs are terminal",
            ),
        ),
        summary_policy=SummaryPolicy(
            summary_stage_code="ready_for_summary",
            outbox_job_code="task_completed_notification",
            rules=(
                SummaryStatusRule(final_status="success", when="all selected rows succeed or no row is selected"),
                SummaryStatusRule(final_status="partial_success", when="some selected rows succeed and others do not"),
                SummaryStatusRule(final_status="failed", when="the table read fails or no selected row succeeds"),
            ),
        ),
        idempotency_policy=(
            IdempotencyRule(
                scope="request",
                key_template="{request_id}:{workflow_code}",
                description="One batch orchestration per task request.",
            ),
            IdempotencyRule(
                scope="source_row",
                key_template="{request_id}:amazon_row_refresh:{source_record_id}:{requested_asin}",
                description="One same-request row refresh job per selected Feishu record.",
            ),
        ),
        timeout_policy=(
            TimeoutRule(
                target_code="feishu_table_read",
                timeout_seconds=180,
                description="Amazon竞品表 batch read timeout.",
            ),
            TimeoutRule(
                target_code="amazon_product_row_refresh",
                timeout_seconds=600,
                description="One Amazon row API pipeline attempt timeout.",
            ),
            TimeoutRule(
                target_code="amazon_product_browser_fetch",
                timeout_seconds=300,
                description="One primary Amazon browser collection timeout.",
            ),
        ),
        summary_contract=contract(
            "refresh_current_amazon_product_table_summary",
            required_field("final_status", "Top-level batch result.", type_hint="str"),
            required_field("row_total_count", "Number of valid T-tagged rows dispatched.", type_hint="int"),
            required_field("row_status_counts", "Terminal row-job status counts.", type_hint="dict[str, int]"),
            required_field("adapter_summary", "Sanitized table selection counters.", type_hint="dict[str, Any]"),
        ),
        error_contract=contract(
            "refresh_current_amazon_product_table_error",
            required_field("error_code", "Stable redacted batch error code.", type_hint="str"),
        ),
        notes=(
            "The parent never parses Amazon pages or writes product facts.",
            "Every selected record uses amazon_product_row_refresh under the same request_id.",
            "browser_required is a primary browser wait state, not a fallback failure state.",
        ),
    )


REFRESH_CURRENT_AMAZON_PRODUCT_TABLE_DEFINITION = (
    build_refresh_current_amazon_product_table_definition()
)


def build_refresh_current_amazon_product_table_workflow(
    *,
    run_mode: str = "draft",
    control_action: str = "submit",
) -> WorkflowSpec:
    del control_action
    return build_formal_task_workflow(workflow_code=WORKFLOW_CODE, run_mode=run_mode)


__all__ = [
    "REFRESH_CURRENT_AMAZON_PRODUCT_TABLE_DEFINITION",
    "WORKFLOW_CODE",
    "build_refresh_current_amazon_product_table_definition",
    "build_refresh_current_amazon_product_table_workflow",
]
