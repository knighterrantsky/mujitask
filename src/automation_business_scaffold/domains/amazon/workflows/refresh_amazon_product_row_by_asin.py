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
    WatchdogRule,
    WorkflowDefinition,
    build_formal_task_workflow,
    contract,
    optional_field,
    required_field,
)
from automation_business_scaffold.domains.amazon.jobs import (
    AMAZON_PRODUCT_BROWSER_FETCH_JOB,
    AMAZON_PRODUCT_ROW_PERSIST_JOB,
    FEISHU_TABLE_READ_JOB,
    FEISHU_TABLE_WRITE_JOB,
    TASK_COMPLETED_NOTIFICATION_JOB,
)


WORKFLOW_CODE = "refresh_amazon_product_row_by_asin"


def build_refresh_amazon_product_row_by_asin_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        task_code=WORKFLOW_CODE,
        workflow_code=WORKFLOW_CODE,
        contract_revision="2026-07-15",
        trigger_modes=("manual", "cli"),
        entry_stage_code="read_amazon_product_row",
        payload_contract=contract(
            "refresh_amazon_product_row_by_asin_payload",
            required_field("table_ref", "Configured Amazon Feishu table alias.", type_hint="str"),
            required_field("source_record_id", "Exact Feishu source record id.", type_hint="str"),
            required_field(
                "table_refs",
                "Required secret-free Amazon table route snapshot from the skill.",
                type_hint="dict[str, str]",
            ),
            notes=("No runtime, browser, credential, or storage configuration is accepted here.",),
        ),
        stages=(
            StageDefinition(
                stage_code="read_amazon_product_row",
                description="Read one Feishu row and validate its Amazon US ASIN identity.",
                execution_mode="worker_jobs",
                enter_condition="the formal request contains table_ref and source_record_id",
                exit_condition="one source row and a valid US ASIN are resolved, or the request fails",
                job_bindings=(
                    StageJobBinding(
                        job_code="feishu_table_read",
                        adapter_code="amazon_product_table_source_adapter",
                        result_consumer="validated source row and resolved table identity",
                    ),
                    StageJobBinding(
                        job_code="feishu_table_write",
                        mapper_code="amazon_product_projection_mapper",
                        result_consumer="status-only terminal identity validation failure",
                        optional=True,
                    ),
                ),
            ),
            StageDefinition(
                stage_code="collect_amazon_product_detail",
                description="Collect the requested Amazon product through the configured browser.",
                execution_mode="worker_jobs",
                enter_condition="the source row contains one validated Amazon US ASIN",
                exit_condition="compact capture references or a controlled terminal failure are stored",
                job_bindings=(
                    StageJobBinding(
                        job_code="feishu_table_write",
                        mapper_code="amazon_product_projection_mapper",
                        result_consumer="collecting or terminal status-only source-row update",
                        optional=True,
                    ),
                    StageJobBinding(
                        job_code="amazon_product_browser_fetch",
                        flow_code="amazon_product_browser_fetch",
                        result_consumer="compact Amazon capture identity, coverage, and artifact references",
                        notes=("This is a primary browser task and never a fallback stage.",),
                    ),
                ),
            ),
            StageDefinition(
                stage_code="persist_amazon_product_detail",
                description="Serially converge Amazon media, facts, projection, and source-row writeback.",
                execution_mode="worker_jobs",
                enter_condition="browser collection produced a persistable compact capture result",
                exit_condition="the row persistence job reaches a terminal business result",
                job_bindings=(
                    StageJobBinding(
                        job_code="feishu_table_write",
                        mapper_code="amazon_product_projection_mapper",
                        result_consumer="persisting or terminal status-only source-row update",
                        optional=True,
                    ),
                    StageJobBinding(
                        job_code="amazon_product_row_persist",
                        flow_code="amazon_product_row_persist",
                        result_consumer="final source-row result and compact fact references",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="ready_for_summary",
                description="Persist the single-row summary and enqueue the final notification.",
                execution_mode="summary",
                enter_condition="no Amazon browser or row-persist child remains active",
                exit_condition="summary, result, and notification outbox are persisted",
                job_bindings=(
                    StageJobBinding(
                        job_code="task_completed_notification",
                        flow_code="summary_renderer",
                        result_consumer="final Amazon row notification",
                    ),
                ),
            ),
        ),
        job_defs=(
            FEISHU_TABLE_READ_JOB,
            FEISHU_TABLE_WRITE_JOB,
            AMAZON_PRODUCT_BROWSER_FETCH_JOB,
            AMAZON_PRODUCT_ROW_PERSIST_JOB,
            TASK_COMPLETED_NOTIFICATION_JOB,
        ),
        transitions=(
            TransitionDefinition(
                from_stage_code="read_amazon_product_row",
                to_stage_code="collect_amazon_product_detail",
                condition="one valid Amazon US source row is available",
            ),
            TransitionDefinition(
                from_stage_code="collect_amazon_product_detail",
                to_stage_code="persist_amazon_product_detail",
                condition="browser collection produced success, partial_success, or unavailable",
            ),
            TransitionDefinition(
                from_stage_code="persist_amazon_product_detail",
                to_stage_code="ready_for_summary",
                condition="the row persistence job is terminal",
            ),
        ),
        summary_policy=SummaryPolicy(
            summary_stage_code="ready_for_summary",
            outbox_job_code="task_completed_notification",
            rules=(
                SummaryStatusRule(
                    final_status="success",
                    when="the row is success or an explicitly unavailable product fact is persisted",
                ),
                SummaryStatusRule(
                    final_status="partial_success",
                    when="facts are persisted but optional media or projection is incomplete",
                ),
                SummaryStatusRule(
                    final_status="failed",
                    when="source identity, browser collection, fact persistence, or writeback fails",
                ),
            ),
        ),
        idempotency_policy=(
            IdempotencyRule(
                scope="request",
                key_template="{request_id}:{workflow_code}",
                description="One logical workflow execution per task request.",
            ),
            IdempotencyRule(
                scope="source_row",
                key_template="{request_id}:{source_record_id}:{requested_asin}",
                description="Browser and persistence children are unique for the source row and ASIN.",
            ),
        ),
        timeout_policy=(
            TimeoutRule(
                target_code="feishu_table_read",
                timeout_seconds=120,
                description="Single Feishu record read timeout.",
            ),
            TimeoutRule(
                target_code="amazon_product_browser_fetch",
                timeout_seconds=300,
                description="Amazon product page browser collection timeout.",
            ),
            TimeoutRule(
                target_code="feishu_table_write",
                timeout_seconds=120,
                description="Amazon stage or terminal status writeback timeout.",
            ),
            TimeoutRule(
                target_code="amazon_product_row_persist",
                timeout_seconds=300,
                description="Media, fact, and Feishu convergence timeout.",
            ),
        ),
        watchdog_policy=(
            WatchdogRule(
                rule_code="retry_stale_amazon_child",
                condition="an Amazon child lease expires before its attempt limit",
                action="requeue the same idempotent child without changing its business run id",
            ),
            WatchdogRule(
                rule_code="fail_exhausted_amazon_child",
                condition="an Amazon child exhausts its attempt or timeout policy",
                action="mark the row failed and release the parent for summary",
            ),
        ),
        summary_contract=contract(
            "refresh_amazon_product_row_by_asin_summary",
            required_field("final_status", "Top-level workflow result status.", type_hint="str"),
            required_field(
                "row_total_count", "Always one after a source row resolves.", type_hint="int"
            ),
            required_field(
                "row_status_counts",
                (
                    "One-hot counts for success, partial_success, unavailable, blocked, "
                    "failed, and skipped."
                ),
                type_hint="dict[str, int]",
            ),
            required_field(
                "aggregate_metrics",
                (
                    "Single-row duration, blocked, parse coverage, media failure, and "
                    "Feishu failure aggregates."
                ),
                type_hint="dict[str, float]",
            ),
            required_field(
                "row_summary",
                "Single-row sanitized identity, timings, coverage, counts, status, and error code.",
                type_hint="dict[str, Any]",
            ),
            required_field(
                "failed_stage",
                "Failed workflow stage, or an empty string for non-failures.",
                type_hint="str",
            ),
            required_field(
                "error_code",
                "Stable redacted error code, or an empty string for non-failures.",
                type_hint="str",
            ),
        ),
        error_contract=contract(
            "refresh_amazon_product_row_by_asin_error",
            required_field("error_code", "Stable redacted error code.", type_hint="str"),
            optional_field("failed_stage", "Stage that failed.", type_hint="str"),
            optional_field("retryable", "Whether Runtime may retry the child.", type_hint="bool"),
        ),
        notes=(
            "The browser stage is the normal Amazon collection path, never fallback_required.",
            "Runtime rows contain compact references only; complete captures remain in object storage.",
            "Batch and keyword-search entrypoints are outside this workflow.",
        ),
    )


REFRESH_AMAZON_PRODUCT_ROW_BY_ASIN_DEFINITION = (
    build_refresh_amazon_product_row_by_asin_definition()
)


def build_refresh_amazon_product_row_by_asin_workflow(
    *,
    run_mode: str = "draft",
    control_action: str = "submit",
) -> WorkflowSpec:
    del control_action
    return build_formal_task_workflow(workflow_code=WORKFLOW_CODE, run_mode=run_mode)


__all__ = [
    "REFRESH_AMAZON_PRODUCT_ROW_BY_ASIN_DEFINITION",
    "WORKFLOW_CODE",
    "build_refresh_amazon_product_row_by_asin_definition",
    "build_refresh_amazon_product_row_by_asin_workflow",
]
