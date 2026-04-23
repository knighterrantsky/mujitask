from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_tiktok_fastmoss_product_ingest_workflow(
    *,
    run_mode: str = "draft",
    control_action: str = "run",
) -> WorkflowSpec:
    del control_action
    return WorkflowSpec(
        workflow_id="tiktok_fastmoss_product_ingest_v1",
        run_mode=run_mode,
        steps=[
            StepDefinition(
                step_id="orchestrate_tiktok_fastmoss_product_ingest",
                action=StepAction(type="orchestrate_tiktok_fastmoss_product_ingest"),
                effects=["write", "upload"],
                postconditions=["result_data_exists:summary.total"],
                outputs=[
                    "summary",
                    "item",
                    "items",
                    "failed_items",
                    "processed_count",
                    "success_count",
                    "failed_count",
                    "daemon_status",
                    "request_id",
                    "request_status",
                    "current_stage",
                    "result",
                    "outbox",
                    "execution_id",
                    "execution_status",
                    "executions",
                    "api_worker_job",
                    "api_worker_jobs",
                    "api_worker_job_summary",
                    "feishu_tk_selection_table_read",
                    "feishu_tk_selection_table_writeback",
                    "feishu_tk_selection_table_read_job_summary",
                    "feishu_tk_selection_table_writeback_job_summary",
                    "product_ingest_job_summary",
                    "parent_updates",
                    "tiktok_browser_fallback_executions",
                    "product_id",
                    "tiktok",
                    "fastmoss",
                    "media_upload",
                    "persisted",
                    "uploaded_media_assets",
                    "fact_entities",
                    "fact_relations",
                    "fact_media_assets",
                    "fact_metric_observations",
                    "raw_api_responses",
                ],
                artifacts={"state_dump": True},
            ),
        ],
    )
