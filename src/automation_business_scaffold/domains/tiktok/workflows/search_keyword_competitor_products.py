from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.contracts.workflow import build_formal_task_workflow
from automation_business_scaffold.domains.tiktok.jobs import (
    COMPETITOR_ROW_REFRESH_JOB,
    FASTMOSS_SECURITY_BROWSER_RESOLVE_JOB,
    KEYWORD_SEED_IMPORT_JOB,
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
    required_field,
)


def build_search_keyword_competitor_products_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        task_code="search_keyword_competitor_products",
        workflow_code="search_keyword_competitor_products",
        contract_revision=DEFAULT_CONTRACT_REVISION,
        trigger_modes=("manual", "cli"),
        entry_stage_code="keyword_seed_import",
        payload_contract=contract(
            "search_keyword_competitor_products_payload",
            required_field("search_query", "Keyword or normalized search query.", type_hint="str"),
            optional_field("filters", "FastMoss search filters.", type_hint="dict[str, Any]"),
            optional_field("output_conditions", "Candidate filtering and dedupe policy.", type_hint="dict[str, Any]"),
            optional_field("total_sales_threshold", "Minimum cumulative sold count threshold.", type_hint="int"),
            optional_field(
                "max_candidates",
                "Upper bound for candidates to process; 0 means unlimited until pagination stops.",
                type_hint="int",
            ),
            optional_field("seed_table_ref", "Target TK competitor table reference.", type_hint="str"),
            optional_field("reply_target", "Reply target used by the final outbox.", type_hint="str"),
        ),
        stages=(
            StageDefinition(
                stage_code="keyword_seed_import",
                description="Search FastMoss candidates and write TK competitor seed rows as one business job.",
                execution_mode="worker_jobs",
                enter_condition="task_request has valid keyword or filter input",
                exit_condition="keyword seed import job is terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="keyword_seed_import",
                        flow_code="keyword_seed_import_flow",
                        mapper_code="competitor_seed_projection_mapper",
                        result_consumer="seed contexts and per-SKU import results",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="fastmoss_security_browser_fallback",
                description="Resolve FastMoss MSG_SAFE_0001 for the original search request in browser.",
                execution_mode="worker_jobs",
                enter_condition="keyword seed import returned fallback_required for MSG_SAFE_0001",
                exit_condition="FastMoss search security browser resolve execution is terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="fastmoss_security_browser_resolve",
                        flow_code="fastmoss_security_browser_resolve",
                        result_consumer="cookie cache metadata and original search verification evidence",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="dispatch_row_refresh_jobs",
                description="Fan out one row refresh job for each newly inserted seed row.",
                execution_mode="executor_action",
                enter_condition="successful seed rows exist",
                exit_condition="competitor row refresh jobs have been created or skipped",
                executor_action_code="dispatch_row_refresh_jobs",
            ),
            StageDefinition(
                stage_code="refresh_competitor_rows",
                description="Refresh each newly inserted competitor seed row through the row-level pipeline.",
                execution_mode="worker_jobs",
                enter_condition="row refresh jobs have been dispatched",
                exit_condition="competitor row refresh jobs are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="competitor_row_refresh",
                        flow_code="competitor_row_refresh_pipeline",
                        result_consumer="row terminal result",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="browser_fallback",
                description="Dispatch browser fallback executions requested by competitor row refresh jobs.",
                execution_mode="worker_jobs",
                enter_condition="competitor row refresh jobs returned fallback_required",
                exit_condition="browser fallback task_executions are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="tiktok_product_browser_fetch",
                        flow_code="tiktok_product_browser_fetch",
                        result_consumer="normalized product result for competitor row refresh after browser fallback",
                    ),
                    StageJobBinding(
                        job_code="fastmoss_security_browser_resolve",
                        flow_code="fastmoss_security_browser_resolve",
                        result_consumer="cookie cache metadata for competitor row refresh after browser fallback",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="ready_for_summary",
                description="Aggregate search, seed, and detail outcomes and enqueue the final notification.",
                execution_mode="summary",
                enter_condition="keyword seed import and final row refresh jobs are terminal with no unresolved browser fallback",
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
            KEYWORD_SEED_IMPORT_JOB,
            FASTMOSS_SECURITY_BROWSER_RESOLVE_JOB,
            COMPETITOR_ROW_REFRESH_JOB,
            TIKTOK_PRODUCT_BROWSER_FETCH_JOB,
            TASK_COMPLETED_NOTIFICATION_JOB,
        ),
        transitions=(
            TransitionDefinition(
                from_stage_code="keyword_seed_import",
                to_stage_code="fastmoss_security_browser_fallback",
                condition="keyword seed import returned fallback_required for FastMoss MSG_SAFE_0001 and fallback has not been attempted",
            ),
            TransitionDefinition(
                from_stage_code="fastmoss_security_browser_fallback",
                to_stage_code="keyword_seed_import",
                condition="browser resolved FastMoss search security verification and refreshed cookie cache",
            ),
            TransitionDefinition(
                from_stage_code="keyword_seed_import",
                to_stage_code="dispatch_row_refresh_jobs",
                condition="keyword seed import job is terminal without FastMoss security fallback requirement",
            ),
            TransitionDefinition(
                from_stage_code="dispatch_row_refresh_jobs",
                to_stage_code="refresh_competitor_rows",
                condition="row refresh fan-out completed or no new seed rows were eligible",
            ),
            TransitionDefinition(
                from_stage_code="refresh_competitor_rows",
                to_stage_code="browser_fallback",
                condition="one or more competitor row refresh jobs returned fallback_required",
            ),
            TransitionDefinition(
                from_stage_code="browser_fallback",
                to_stage_code="refresh_competitor_rows",
                condition="browser fallback task_executions produced row inputs for the same competitor row stage",
            ),
            TransitionDefinition(
                from_stage_code="refresh_competitor_rows",
                to_stage_code="ready_for_summary",
                condition="competitor row refresh jobs are terminal without browser fallback requirement",
            ),
        ),
        summary_policy=notification_summary_policy(
            SummaryStatusRule(
                final_status="success",
                when="search, seed insert, collection, fact persistence, and writeback completed without failed products",
            ),
            SummaryStatusRule(
                final_status="partial_success",
                when="at least one candidate reached persisted facts or writeback, but some products or seed writes failed",
            ),
            SummaryStatusRule(
                final_status="failed",
                when="no usable candidate produced a persisted or written result, or orchestration failed irrecoverably",
            ),
            notes=(
                "Summary should preserve search counts, seed insert counts, and product-level terminal counts.",
            ),
        ),
        idempotency_policy=table_workflow_idempotency_rules(
            request_scope="{request_id}:{workflow_code}:{search_digest}",
            row_scope="{request_id}:{stage_code}:{product_id_or_seed_record_id}",
        ),
        timeout_policy=table_workflow_timeout_rules(include_browser=True),
        watchdog_policy=standard_watchdog_rules(include_browser=True),
        summary_contract=STANDARD_SUMMARY_CONTRACT,
        error_contract=STANDARD_ERROR_CONTRACT,
        notes=(
            "Seed row creation is intentionally modeled as feishu_table_write plus competitor_seed_projection_mapper.",
            "Search candidate normalization stays in executor because it is lightweight, idempotent, and workflow-specific.",
        ),
    )


SEARCH_KEYWORD_COMPETITOR_PRODUCTS_DEFINITION = (
    build_search_keyword_competitor_products_definition()
)


def build_search_keyword_competitor_products_workflow(
    *,
    run_mode: str = "draft",
    control_action: str = "submit",
) -> WorkflowSpec:
    del control_action
    return build_formal_task_workflow(
        workflow_code="search_keyword_competitor_products",
        run_mode=run_mode,
    )
