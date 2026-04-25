from automation_business_scaffold.apps.cli.main import list_registered_tasks
from automation_business_scaffold.domains.tiktok.tasks import (
    RefreshCompetitorRowByUrlTask,
    RefreshCurrentCompetitorTableTask,
    SearchKeywordCompetitorProductsTask,
    SyncTKInfluencerPoolTask,
    TikTokFastMossProductIngestTask,
)


def test_refresh_competitor_row_by_url_workflow_uses_formal_runtime_shell():
    task = RefreshCompetitorRowByUrlTask()

    workflow = task.build_workflow({"control_action": "submit"})

    assert workflow.workflow_id == "refresh_competitor_row_by_url"
    assert workflow.run_mode == "full_auto"
    assert [step.step_id for step in workflow.steps] == ["dispatch_task_request"]


def test_refresh_current_competitor_table_workflow_uses_formal_runtime_shell():
    task = RefreshCurrentCompetitorTableTask()

    workflow = task.build_workflow({})

    assert workflow.workflow_id == "refresh_current_competitor_table"
    assert workflow.run_mode == "full_auto"
    assert [step.step_id for step in workflow.steps] == ["dispatch_task_request"]


def test_search_keyword_competitor_products_workflow_uses_formal_runtime_shell():
    task = SearchKeywordCompetitorProductsTask()

    workflow = task.build_workflow({})

    assert workflow.workflow_id == "search_keyword_competitor_products"
    assert workflow.run_mode == "full_auto"
    assert [step.step_id for step in workflow.steps] == ["dispatch_task_request"]


def test_sync_tk_influencer_pool_workflow_uses_formal_runtime_shell():
    task = SyncTKInfluencerPoolTask()

    workflow = task.build_workflow({"control_action": "submit"})

    assert workflow.workflow_id == "sync_tk_influencer_pool"
    assert workflow.run_mode == "full_auto"
    assert [step.step_id for step in workflow.steps] == ["dispatch_task_request"]


def test_tiktok_fastmoss_product_ingest_workflow_uses_formal_runtime_shell():
    task = TikTokFastMossProductIngestTask()

    workflow = task.build_workflow({"control_action": "submit"})

    assert workflow.workflow_id == "tiktok_fastmoss_product_ingest"
    assert workflow.run_mode == "full_auto"
    assert [step.step_id for step in workflow.steps] == ["dispatch_task_request"]


def test_cli_runner_lists_only_formal_runtime_tasks():
    assert list_registered_tasks() == [
        {
            "name": "refresh_competitor_row_by_url",
            "description": "Submit, inspect, or advance a competitor row refresh runtime request located by product URL.",
        },
        {
            "name": "refresh_current_competitor_table",
            "description": "Submit, inspect, or advance the competitor table refresh runtime request.",
        },
        {
            "name": "search_keyword_competitor_products",
            "description": "Submit, inspect, or advance the keyword competitor search runtime request.",
        },
        {
            "name": "sync_tk_influencer_pool",
            "description": "Submit, inspect, or advance the influencer pool sync runtime request.",
        },
        {
            "name": "tiktok_fastmoss_product_ingest",
            "description": "Submit, inspect, or advance the TikTok plus FastMoss product ingest runtime request.",
        },
    ]
