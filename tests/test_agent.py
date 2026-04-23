# Platform-managed: this test protects the scaffold shell entrypoint and task exposure.

from fastapi.testclient import TestClient

from automation_business_scaffold.agent import app


def test_agent_lists_current_tasks():
    client = TestClient(app)

    response = client.get("/tasks")

    assert response.status_code == 200
    assert response.json() == {
        "tasks": [
            "fastmoss_keyword_candidate_discovery",
            "fastmoss_login_check",
            "fastmoss_product_sales_snapshot",
            "feishu_clear_row_by_url",
            "feishu_pending_rows_scan",
            "feishu_seed_row_insert",
            "feishu_single_row_update",
            "refresh_current_competitor_table",
            "search_keyword_competitor_products",
            "sync_tk_influencer_pool",
            "tiktok_feishu_single_sync",
            "tiktok_product_link_cleanup",
        ]
    }
