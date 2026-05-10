from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.contracts.workflow import build_formal_task_workflow
from automation_business_scaffold.domains.tiktok.jobs import (
    FASTMOSS_SECURITY_BROWSER_RESOLVE_JOB,
    FEISHU_TABLE_READ_JOB,
    SELECTION_ROW_REFRESH_JOB,
    TASK_COMPLETED_NOTIFICATION_JOB,
    TIKTOK_PRODUCT_BROWSER_FETCH_JOB,
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
)


def build_tiktok_fastmoss_product_ingest_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        task_code="tiktok_fastmoss_product_ingest",
        workflow_code="tiktok_fastmoss_product_ingest",
        contract_revision=DEFAULT_CONTRACT_REVISION,
        trigger_modes=("manual", "schedule", "webhook", "cli"),
        entry_stage_code="read_selection_rows",
        payload_contract=contract(
            "tiktok_fastmoss_product_ingest_payload",
            optional_field("product_url", "Direct-ingest TikTok product URL.", type_hint="str"),
            optional_field("product_id", "Direct-ingest normalized product id.", type_hint="str"),
            optional_field("selection_table_ref", "Optional TK selection table reference.", type_hint="str"),
            optional_field("selection_record_id", "Optional source selection record id.", type_hint="str"),
            optional_field(
                "writeback_enabled",
                "Whether selection-table projection writeback should run when source context exists.",
                type_hint="bool",
            ),
            optional_field(
                "fallback_allowed",
                "Whether browser fallback is allowed for the TikTok request path.",
                type_hint="bool",
            ),
            optional_field("reply_target", "Reply target used by the final outbox.", type_hint="str"),
            notes=(
                "Direct ingest should provide product_url or product_id.",
                "Selection-table mode should provide selection_table_ref.",
            ),
        ),
        stages=(
            StageDefinition(
                stage_code="read_selection_rows",
                description="Read and normalize candidate selection rows from TK selection table.",
                execution_mode="worker_jobs",
                enter_condition="selection-table mode is enabled or the stage is explicitly skipped for direct ingest",
                exit_condition="selection source rows have been read and normalized for executor fan-out",
                job_bindings=(
                    StageJobBinding(
                        job_code="feishu_table_read",
                        adapter_code="selection_table_source_adapter",
                        result_consumer="executor fan-out target rows",
                        optional=True,
                    ),
                ),
                notes=(
                    "Direct ingest mode should mark this stage skipped rather than branching around the workflow definition.",
                ),
            ),
            StageDefinition(
                stage_code="dispatch_selection_row_refresh",
                description="Fan out one row-level pipeline job for each candidate selection row.",
                execution_mode="executor_action",
                enter_condition="candidate selection rows are available from read or direct-ingest context",
                exit_condition="per-row selection_row_refresh jobs have been created or skipped",
                executor_action_code="fanout_selection_rows",
                notes=(
                    "This stage remains an executor action because it is lightweight and idempotent.",
                ),
            ),
            StageDefinition(
                stage_code="collect_selection_rows",
                description="Run one row-level selection refresh pipeline job per candidate row.",
                execution_mode="worker_jobs",
                enter_condition="row-level selection_row_refresh jobs have been dispatched",
                exit_condition="selection row pipeline jobs are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="selection_row_refresh",
                        flow_code="selection_row_pipeline",
                        result_consumer="row terminal result",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="selection_row_browser_fallback",
                description="Dispatch browser fallback executions requested by selection row refresh jobs.",
                execution_mode="worker_jobs",
                enter_condition="selection_row_refresh jobs returned fallback_required",
                exit_condition="selection row browser fallback task_executions are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="tiktok_product_browser_fetch",
                        flow_code="tiktok_product_browser_fetch",
                        result_consumer="normalized product result for row refresh after browser fallback",
                    ),
                    StageJobBinding(
                        job_code="fastmoss_security_browser_resolve",
                        flow_code="fastmoss_security_browser_resolve",
                        result_consumer="cookie cache metadata for row refresh after browser fallback",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="ready_for_summary",
                description="Aggregate row outcomes and enqueue the final notification payload.",
                execution_mode="summary",
                enter_condition="all child jobs for selection row refresh are terminal",
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
            SELECTION_ROW_REFRESH_JOB,
            TIKTOK_PRODUCT_BROWSER_FETCH_JOB,
            FASTMOSS_SECURITY_BROWSER_RESOLVE_JOB,
            TASK_COMPLETED_NOTIFICATION_JOB,
        ),
        transitions=(
            TransitionDefinition(
                from_stage_code="read_selection_rows",
                to_stage_code="dispatch_selection_row_refresh",
                condition="feishu_table_read is terminal or explicitly skipped for direct ingest",
            ),
            TransitionDefinition(
                from_stage_code="dispatch_selection_row_refresh",
                to_stage_code="collect_selection_rows",
                condition="row fan-out has completed or no rows require refresh",
            ),
            TransitionDefinition(
                from_stage_code="collect_selection_rows",
                to_stage_code="selection_row_browser_fallback",
                condition="selection row pipeline jobs requested browser fallback",
            ),
            TransitionDefinition(
                from_stage_code="collect_selection_rows",
                to_stage_code="ready_for_summary",
                condition="selection row pipeline jobs are terminal and no browser fallback is required",
            ),
            TransitionDefinition(
                from_stage_code="selection_row_browser_fallback",
                to_stage_code="collect_selection_rows",
                condition="browser fallback task_executions produced row inputs for the same selection row stage",
            ),
            TransitionDefinition(
                from_stage_code="selection_row_browser_fallback",
                to_stage_code="ready_for_summary",
                condition="browser fallback task_executions are terminal but no row can continue after browser",
            ),
        ),
        summary_policy=notification_summary_policy(
            SummaryStatusRule(
                final_status="success",
                when="all dispatched selection row pipeline jobs completed without failed rows",
            ),
            SummaryStatusRule(
                final_status="partial_success",
                when="at least one selection row refreshed successfully but some row pipelines were partial or failed",
            ),
            SummaryStatusRule(
                final_status="failed",
                when="no selection row pipeline produced a successful writeback result or orchestration failed irrecoverably",
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
            "Direct ingest and selection-table mode share the same workflow definition; executor should skip read/dispatch stages when they are not applicable.",
            "Each selection row runs a full serial pipeline (TikTok → FastMoss → fact DB → Feishu writeback) inside selection_row_refresh.",
            "Row browser fallback is owned by task_execution/browser-runloop; selection_row_refresh may only request fallback and must consume persisted browser output by reference.",
        ),
    )


TIKTOK_FASTMOSS_PRODUCT_INGEST_DEFINITION = build_tiktok_fastmoss_product_ingest_definition()


def build_tiktok_fastmoss_product_ingest_workflow(
    *,
    run_mode: str = "draft",
    control_action: str = "submit",
) -> WorkflowSpec:
    del control_action
    return build_formal_task_workflow(
        workflow_code="tiktok_fastmoss_product_ingest",
        run_mode=run_mode,
    )
