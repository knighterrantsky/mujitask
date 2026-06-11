from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.contracts.workflow import build_formal_task_workflow
from automation_business_scaffold.control_plane.runtime_config.settings import (
    SELECTION_KEYWORD_TASK_CODE,
)
from automation_business_scaffold.domains.tiktok.jobs import (
    FASTMOSS_SECURITY_BROWSER_RESOLVE_JOB,
    KEYWORD_SEED_IMPORT_JOB,
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
    required_field,
)


WORKFLOW_CODE = SELECTION_KEYWORD_TASK_CODE


def build_search_keyword_selection_products_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        task_code=SELECTION_KEYWORD_TASK_CODE,
        workflow_code=WORKFLOW_CODE,
        contract_revision=DEFAULT_CONTRACT_REVISION,
        trigger_modes=("manual", "cli"),
        entry_stage_code="keyword_seed_import",
        payload_contract=contract(
            "search_keyword_selection_products_payload",
            required_field("search_query", "Keyword or normalized search query.", type_hint="str"),
            optional_field("filters", "FastMoss search filters.", type_hint="dict[str, Any]"),
            optional_field("output_conditions", "Candidate filtering and dedupe policy.", type_hint="dict[str, Any]"),
            optional_field(
                "max_candidates",
                "Upper bound for candidates to process; 0 means unlimited until pagination stops.",
                type_hint="int",
            ),
            optional_field("sales_7d_threshold", "Minimum 7-day sales threshold; defaults to 500.", type_hint="int"),
            optional_field("total_sales_threshold", "Minimum cumulative sold count threshold.", type_hint="int"),
            optional_field("product_price_threshold", "Minimum product price using the FastMoss range maximum; defaults to 10.99.", type_hint="float"),
            optional_field("selection_table_ref", "Target TK selection table reference.", type_hint="str"),
            optional_field("seed_table_ref", "Compatible target TK selection table reference alias.", type_hint="str"),
            optional_field("reply_target", "Reply target used by the final outbox.", type_hint="str"),
        ),
        stages=(
            StageDefinition(
                stage_code="keyword_seed_import",
                description="Search FastMoss candidates and write TK selection seed rows as one business job.",
                execution_mode="worker_jobs",
                enter_condition="task_request has valid keyword or filter input",
                exit_condition="keyword seed import job is terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="keyword_seed_import",
                        flow_code="keyword_seed_import_flow",
                        mapper_code="selection_seed_projection_mapper",
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
                stage_code="dispatch_selection_row_refresh_jobs",
                description="Fan out one selection row refresh job for each newly inserted seed row.",
                execution_mode="executor_action",
                enter_condition="successful seed rows exist",
                exit_condition="selection row refresh jobs have been created or skipped",
                executor_action_code="dispatch_selection_row_refresh_jobs",
            ),
            StageDefinition(
                stage_code="refresh_selection_rows",
                description="Refresh each newly inserted selection seed row through the selection row-level pipeline.",
                execution_mode="worker_jobs",
                enter_condition="selection row refresh jobs have been dispatched",
                exit_condition="selection row refresh jobs are terminal",
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
                enter_condition="selection row refresh jobs returned fallback_required",
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
                description="Aggregate search, seed, and detail outcomes and enqueue the final notification.",
                execution_mode="summary",
                enter_condition="keyword seed import and selection row refresh jobs are terminal",
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
            TIKTOK_PRODUCT_BROWSER_FETCH_JOB,
            SELECTION_ROW_REFRESH_JOB,
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
                to_stage_code="dispatch_selection_row_refresh_jobs",
                condition="keyword seed import job is terminal without FastMoss security fallback requirement",
            ),
            TransitionDefinition(
                from_stage_code="dispatch_selection_row_refresh_jobs",
                to_stage_code="refresh_selection_rows",
                condition="selection row refresh fan-out completed or no new seed rows were eligible",
            ),
            TransitionDefinition(
                from_stage_code="refresh_selection_rows",
                to_stage_code="selection_row_browser_fallback",
                condition="one or more selection row refresh jobs returned fallback_required",
            ),
            TransitionDefinition(
                from_stage_code="selection_row_browser_fallback",
                to_stage_code="refresh_selection_rows",
                condition="browser fallback task_executions produced row inputs for the same selection row stage",
            ),
            TransitionDefinition(
                from_stage_code="refresh_selection_rows",
                to_stage_code="ready_for_summary",
                condition="selection row refresh jobs are terminal without browser fallback requirement",
            ),
        ),
        summary_policy=notification_summary_policy(
            SummaryStatusRule(
                final_status="success",
                when="search, seed insert, selection row refresh, fact persistence, and writeback completed without failed products",
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
            "Seed row creation is intentionally modeled as feishu_table_write plus selection_seed_projection_mapper.",
            "Search list filtering applies before selection seed insertion to reduce downstream refresh volume.",
        ),
    )


SEARCH_KEYWORD_SELECTION_PRODUCTS_DEFINITION = (
    build_search_keyword_selection_products_definition()
)


def build_search_keyword_selection_products_workflow(
    *,
    run_mode: str = "draft",
    control_action: str = "submit",
) -> WorkflowSpec:
    del control_action
    return build_formal_task_workflow(
        workflow_code="search_keyword_selection_products",
        run_mode=run_mode,
    )
