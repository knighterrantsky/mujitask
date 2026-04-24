from __future__ import annotations

from automation_business_scaffold.business.workflow_defs.job_catalog import (
    COMMON_TASK_COMPLETED_NOTIFICATION_JOB,
    DEFAULT_CONTRACT_REVISION,
    FACT_BUNDLE_UPSERT_JOB,
    FASTMOSS_PRODUCT_FETCH_JOB,
    FASTMOSS_PRODUCT_SEARCH_JOB,
    FEISHU_TABLE_WRITE_JOB,
    MEDIA_ASSET_SYNC_JOB,
    STANDARD_ERROR_CONTRACT,
    STANDARD_SUMMARY_CONTRACT,
    TIKTOK_PRODUCT_BROWSER_FETCH_JOB,
    TIKTOK_PRODUCT_REQUEST_FETCH_JOB,
    notification_summary_policy,
    standard_watchdog_rules,
    table_workflow_idempotency_rules,
    table_workflow_timeout_rules,
)
from automation_business_scaffold.business.workflow_defs.models import (
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
        entry_stage_code="search_product_candidates",
        payload_contract=contract(
            "search_keyword_competitor_products_payload",
            required_field("search_query", "Keyword or normalized search query.", type_hint="str"),
            optional_field("filters", "FastMoss search filters.", type_hint="dict[str, Any]"),
            optional_field("output_conditions", "Candidate filtering and dedupe policy.", type_hint="dict[str, Any]"),
            optional_field("max_candidates", "Upper bound for candidates to process.", type_hint="int"),
            optional_field("seed_table_ref", "Target TK competitor table reference.", type_hint="str"),
            optional_field("reply_target", "Reply target used by the final outbox.", type_hint="str"),
        ),
        stages=(
            StageDefinition(
                stage_code="search_product_candidates",
                description="Search FastMoss for normalized product candidates.",
                execution_mode="worker_jobs",
                enter_condition="task_request has valid keyword or filter input",
                exit_condition="candidate search job is terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="fastmoss_product_search",
                        flow_code="fastmoss_product_search_api_flow",
                        result_consumer="candidate normalizer and output conditions",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="process_product_candidates",
                description="Normalize, dedupe, and filter search candidates before seed creation.",
                execution_mode="executor_action",
                enter_condition="FastMoss candidate search has completed successfully",
                exit_condition="seed row payloads have been produced or all candidates were skipped",
                executor_action_code="normalize_product_candidates",
            ),
            StageDefinition(
                stage_code="insert_seed_rows",
                description="Insert or upsert competitor seed rows into TK competitor table.",
                execution_mode="worker_jobs",
                enter_condition="seed payloads are available for Feishu insertion",
                exit_condition="seed row write jobs are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="feishu_table_write",
                        mapper_code="competitor_seed_projection_mapper",
                        result_consumer="created Feishu record ids",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="dispatch_product_collection",
                description="Fan out request-first product collection jobs from successful seed rows.",
                execution_mode="executor_action",
                enter_condition="successful seed rows exist",
                exit_condition="product collection jobs have been created or skipped",
                executor_action_code="fanout_seed_rows",
            ),
            StageDefinition(
                stage_code="collect_product_data",
                description="Collect TikTok and FastMoss product data for seeded competitor rows.",
                execution_mode="worker_jobs",
                enter_condition="product collection jobs have been dispatched",
                exit_condition="collection jobs are terminal or request browser fallback",
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
                description="Execute browser fallback for seeded rows that require TikTok browser recovery.",
                execution_mode="worker_jobs",
                enter_condition="at least one TikTok request result returned fallback_required=true",
                exit_condition="browser fallback jobs are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="tiktok_product_browser_fetch",
                        flow_code="browser_product_page_flow",
                        result_consumer="normalized product result",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="sync_media",
                description="Sync product media and derived assets before fact persistence.",
                execution_mode="worker_jobs",
                enter_condition="normalized product results include media assets",
                exit_condition="media sync jobs are terminal or skipped",
                job_bindings=(
                    StageJobBinding(
                        job_code="media_asset_sync",
                        flow_code="media_object_store_flow",
                        result_consumer="media facts and writeback projection",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="persist_facts",
                description="Persist normalized product entities, relations, and observations to Fact DB.",
                execution_mode="worker_jobs",
                enter_condition="collection results and optional media refs are available",
                exit_condition="fact upsert jobs are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="fact_bundle_upsert",
                        mapper_code="competitor_fact_relation_mapper",
                        result_consumer="competitor writeback projection",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="writeback_competitor_rows",
                description="Write detail projection back into TK competitor rows.",
                execution_mode="worker_jobs",
                enter_condition="fact upsert has produced writeback-ready projection data",
                exit_condition="detail writeback jobs are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="feishu_table_write",
                        mapper_code="competitor_table_projection_mapper",
                        result_consumer="detail terminal result",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="ready_for_summary",
                description="Aggregate search, seed, and detail outcomes and enqueue the final notification.",
                execution_mode="summary",
                enter_condition="all child jobs across search, seed, collection, and writeback are terminal",
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
            FASTMOSS_PRODUCT_SEARCH_JOB,
            FEISHU_TABLE_WRITE_JOB,
            TIKTOK_PRODUCT_REQUEST_FETCH_JOB,
            FASTMOSS_PRODUCT_FETCH_JOB,
            TIKTOK_PRODUCT_BROWSER_FETCH_JOB,
            MEDIA_ASSET_SYNC_JOB,
            FACT_BUNDLE_UPSERT_JOB,
            COMMON_TASK_COMPLETED_NOTIFICATION_JOB,
        ),
        transitions=(
            TransitionDefinition(
                from_stage_code="search_product_candidates",
                to_stage_code="process_product_candidates",
                condition="fastmoss_product_search is terminal and candidate payload is available",
            ),
            TransitionDefinition(
                from_stage_code="process_product_candidates",
                to_stage_code="insert_seed_rows",
                condition="candidate filtering produced at least one seed payload or an empty seed set was recorded",
            ),
            TransitionDefinition(
                from_stage_code="insert_seed_rows",
                to_stage_code="dispatch_product_collection",
                condition="seed row writes are terminal",
            ),
            TransitionDefinition(
                from_stage_code="dispatch_product_collection",
                to_stage_code="collect_product_data",
                condition="product collection fan-out completed or no seed rows were eligible",
            ),
            TransitionDefinition(
                from_stage_code="collect_product_data",
                to_stage_code="browser_fallback",
                condition="at least one tiktok_product_request_fetch result requires browser fallback",
                transition_type="conditional",
            ),
            TransitionDefinition(
                from_stage_code="collect_product_data",
                to_stage_code="sync_media",
                condition="product collection jobs are terminal and no browser fallback is pending",
                transition_type="conditional",
            ),
            TransitionDefinition(
                from_stage_code="browser_fallback",
                to_stage_code="sync_media",
                condition="browser fallback jobs are terminal",
            ),
            TransitionDefinition(
                from_stage_code="sync_media",
                to_stage_code="persist_facts",
                condition="media sync jobs are terminal or skipped",
            ),
            TransitionDefinition(
                from_stage_code="persist_facts",
                to_stage_code="writeback_competitor_rows",
                condition="fact upsert jobs are terminal",
            ),
            TransitionDefinition(
                from_stage_code="writeback_competitor_rows",
                to_stage_code="ready_for_summary",
                condition="competitor detail writeback jobs are terminal",
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
