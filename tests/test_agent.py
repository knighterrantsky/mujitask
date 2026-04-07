# Platform-managed: this test protects the scaffold shell entrypoint and task exposure.

from fastapi.testclient import TestClient

from automation_business_scaffold.agent import app


def test_agent_lists_demo_task():
    client = TestClient(app)

    response = client.get("/tasks")

    assert response.status_code == 200
    assert response.json() == {
        "tasks": [
            "fastmoss_keyword_candidate_discovery",
            "fastmoss_product_sales_snapshot",
            "feishu_pending_rows_scan",
            "feishu_seed_row_insert",
            "feishu_single_row_update",
            "source_to_target_publish_demo",
            "tiktok_feishu_batch_sync",
            "tiktok_feishu_single_sync",
            "tiktok_product_link_cleanup",
            "tiktok_product_to_feishu",
        ]
    }
