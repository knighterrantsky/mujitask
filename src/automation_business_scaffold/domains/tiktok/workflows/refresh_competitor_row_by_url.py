from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.contracts.workflow import build_formal_task_workflow
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

WORKFLOW_CODE = "refresh_competitor_row_by_url"


def build_refresh_competitor_row_by_url_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        task_code=WORKFLOW_CODE,
        workflow_code=WORKFLOW_CODE,
        contract_revision=DEFAULT_CONTRACT_REVISION,
        trigger_modes=("manual", "cli"),
        entry_stage_code="read_competitor_rows",
        payload_contract=contract(
            "refresh_competitor_row_by_url_payload",
            required_field(
                "source_table_ref",
                "Source TK competitor table reference.",
                type_hint="str",
            ),
            required_field(
                "product_url",
                "TikTok product URL used to locate the matching competitor row.",
                type_hint="str",
            ),
            optional_field("view_ref", "Optional Feishu view identifier.", type_hint="str"),
            optional_field(
                "fallback_allowed",
                "Whether browser fallback is allowed for the TikTok request path.",
                type_hint="bool",
            ),
            optional_field("reply_target", "Reply target used by the final outbox.", type_hint="str"),
        ),
        stages=(
            StageDefinition(
                stage_code="read_competitor_rows",
                description="Read TK competitor table rows and locate the single row matching the provided product URL.",
                execution_mode="worker_jobs",
                enter_condition="task_request has source table context and target product URL",
                exit_condition="the matching competitor row has been resolved or the request is terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="feishu_table_read",
                        adapter_code="competitor_table_source_adapter",
                        result_consumer="matched competitor row context",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="dispatch_product_collection",
                description="Dispatch one row-level competitor refresh pipeline job for the matched row.",
                execution_mode="executor_action",
                enter_condition="a single competitor row has been resolved from the source table",
                exit_condition="one row-level competitor refresh job has been created or the request is terminal",
                executor_action_code="fanout_competitor_rows",
            ),
            StageDefinition(
                stage_code="collect_product_data",
                description="Run the matched competitor row through the row-level refresh pipeline.",
                execution_mode="worker_jobs",
                enter_condition="the matched competitor row refresh job has been dispatched",
                exit_condition="the competitor row refresh job is terminal",
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
                description="Aggregate the single-row refresh outcome and enqueue the final notification payload.",
                execution_mode="summary",
                enter_condition="the competitor row refresh job is terminal or the request failed before dispatch",
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
                condition="the product URL has resolved to exactly one competitor row",
            ),
            TransitionDefinition(
                from_stage_code="dispatch_product_collection",
                to_stage_code="collect_product_data",
                condition="the single competitor row refresh job has been dispatched",
            ),
            TransitionDefinition(
                from_stage_code="collect_product_data",
                to_stage_code="ready_for_summary",
                condition="the competitor row refresh job is terminal",
            ),
        ),
        summary_policy=notification_summary_policy(
            SummaryStatusRule(
                final_status="success",
                when="the matched competitor row refresh pipeline completed successfully",
            ),
            SummaryStatusRule(
                final_status="partial_success",
                when="the matched competitor row completed with partial success",
            ),
            SummaryStatusRule(
                final_status="failed",
                when="the product URL did not resolve to one competitor row or the row refresh failed",
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
            "This workflow provides the formal URL -> competitor row mapping entrypoint for single-row competitor refresh.",
            "The matched row still runs through competitor_row_refresh so handler granularity does not become job granularity.",
        ),
    )


REFRESH_COMPETITOR_ROW_BY_URL_DEFINITION = build_refresh_competitor_row_by_url_definition()


def build_refresh_competitor_row_by_url_workflow(
    *,
    run_mode: str = "draft",
    control_action: str = "submit",
) -> WorkflowSpec:
    del control_action
    return build_formal_task_workflow(
        workflow_code=WORKFLOW_CODE,
        run_mode=run_mode,
    )
