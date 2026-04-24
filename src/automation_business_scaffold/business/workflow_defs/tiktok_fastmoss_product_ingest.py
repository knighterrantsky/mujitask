from __future__ import annotations

from .job_catalog import (
    COMMON_TASK_COMPLETED_NOTIFICATION_JOB,
    DEFAULT_CONTRACT_REVISION,
    FACT_BUNDLE_UPSERT_JOB,
    FASTMOSS_PRODUCT_FETCH_JOB,
    FEISHU_TABLE_READ_JOB,
    FEISHU_TABLE_WRITE_JOB,
    MEDIA_ASSET_SYNC_JOB,
    STANDARD_ERROR_CONTRACT,
    STANDARD_SUMMARY_CONTRACT,
    TIKTOK_PRODUCT_BROWSER_FETCH_JOB,
    TIKTOK_PRODUCT_REQUEST_FETCH_JOB,
    ingest_idempotency_rules,
    notification_summary_policy,
    single_product_timeout_rules,
    standard_watchdog_rules,
)
from .models import (
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
                "Selection-table mode should provide selection_table_ref and selection_record_id.",
            ),
        ),
        stages=(
            StageDefinition(
                stage_code="read_selection_rows",
                description="Optionally read TK selection rows and derive product + writeback context.",
                execution_mode="worker_jobs",
                enter_condition="selection-table mode is enabled or the stage is explicitly skipped for direct ingest",
                exit_condition="selection source context is available or the stage has been skipped",
                job_bindings=(
                    StageJobBinding(
                        job_code="feishu_table_read",
                        adapter_code="selection_table_source_adapter",
                        result_consumer="product collection payload and writeback context",
                        optional=True,
                    ),
                ),
                notes=(
                    "Direct ingest mode should mark this stage skipped rather than branching around the workflow definition.",
                ),
            ),
            StageDefinition(
                stage_code="collect_product_data",
                description="Collect TikTok request-first data and FastMoss product facts for a single product.",
                execution_mode="worker_jobs",
                enter_condition="product identity is available from direct input or selection source context",
                exit_condition="product collection jobs are terminal or request browser fallback",
                job_bindings=(
                    StageJobBinding(
                        job_code="tiktok_product_request_fetch",
                        flow_code="tiktok_request_flow",
                        result_consumer="normalized result or fallback decision",
                    ),
                    StageJobBinding(
                        job_code="fastmoss_product_fetch",
                        flow_code="fastmoss_product_flow",
                        result_consumer="product facts and metrics",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="browser_fallback",
                description="Execute browser fallback when the TikTok request path cannot recover on its own.",
                execution_mode="worker_jobs",
                enter_condition="tiktok_product_request_fetch returned fallback_required=true and fallback is allowed",
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
                description="Sync media assets associated with the normalized product result.",
                execution_mode="worker_jobs",
                enter_condition="normalized product result includes media assets",
                exit_condition="media sync jobs are terminal or skipped",
                job_bindings=(
                    StageJobBinding(
                        job_code="media_asset_sync",
                        flow_code="object_store_upload_flow",
                        result_consumer="media fact refs and artifact_object refs",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="persist_facts",
                description="Persist normalized product facts, relations, and observations to Fact DB.",
                execution_mode="worker_jobs",
                enter_condition="normalized product result and optional media refs are available",
                exit_condition="fact upsert jobs are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="fact_bundle_upsert",
                        mapper_code="selection_fact_relation_mapper",
                        result_consumer="selection writeback projection or summary",
                    ),
                ),
            ),
            StageDefinition(
                stage_code="writeback_selection_rows",
                description="Optionally project ingest results back to TK selection rows.",
                execution_mode="worker_jobs",
                enter_condition="selection source context exists and writeback is enabled",
                exit_condition="selection writeback jobs are terminal",
                job_bindings=(
                    StageJobBinding(
                        job_code="feishu_table_write",
                        mapper_code="selection_table_projection_mapper",
                        result_consumer="source row updated",
                        optional=True,
                    ),
                ),
            ),
            StageDefinition(
                stage_code="ready_for_summary",
                description="Aggregate ingest results and enqueue the final notification payload.",
                execution_mode="summary",
                enter_condition="all child jobs for this product ingest are terminal",
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
            COMMON_TASK_COMPLETED_NOTIFICATION_JOB,
        ),
        transitions=(
            TransitionDefinition(
                from_stage_code="read_selection_rows",
                to_stage_code="collect_product_data",
                condition="selection read is terminal or explicitly skipped for direct ingest",
            ),
            TransitionDefinition(
                from_stage_code="collect_product_data",
                to_stage_code="browser_fallback",
                condition="tiktok_product_request_fetch requested browser fallback",
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
                to_stage_code="writeback_selection_rows",
                condition="selection source context exists and writeback is enabled",
                transition_type="conditional",
            ),
            TransitionDefinition(
                from_stage_code="persist_facts",
                to_stage_code="ready_for_summary",
                condition="selection writeback is not required for this ingest request",
                transition_type="conditional",
            ),
            TransitionDefinition(
                from_stage_code="writeback_selection_rows",
                to_stage_code="ready_for_summary",
                condition="selection writeback jobs are terminal",
            ),
        ),
        summary_policy=notification_summary_policy(
            SummaryStatusRule(
                final_status="success",
                when="normalized product facts were persisted and optional writeback completed without failed child jobs",
            ),
            SummaryStatusRule(
                final_status="partial_success",
                when="normalized product facts were persisted but some optional media, FastMoss, or writeback steps failed",
            ),
            SummaryStatusRule(
                final_status="failed",
                when="no normalized product facts were persisted or request/browser collection failed irrecoverably",
            ),
            notes=(
                "Summary should preserve the request-first versus browser-fallback path that won for the final normalized result.",
            ),
        ),
        idempotency_policy=ingest_idempotency_rules(),
        timeout_policy=single_product_timeout_rules(include_browser=True),
        watchdog_policy=standard_watchdog_rules(include_browser=True),
        summary_contract=STANDARD_SUMMARY_CONTRACT,
        error_contract=STANDARD_ERROR_CONTRACT,
        notes=(
            "Direct ingest and selection-table mode share the same workflow definition; executor should skip read/write stages when they are not applicable.",
        ),
    )


TIKTOK_FASTMOSS_PRODUCT_INGEST_DEFINITION = build_tiktok_fastmoss_product_ingest_definition()
