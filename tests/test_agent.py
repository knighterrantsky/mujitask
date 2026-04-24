# Platform-managed: this test protects the scaffold shell entrypoint and task exposure.

from fastapi.testclient import TestClient

from automation_business_scaffold.agent import app


def test_agent_lists_current_tasks():
    client = TestClient(app)

    response = client.get("/tasks")

    assert response.status_code == 200
    assert response.json() == {
        "tasks": [
            "refresh_current_competitor_table",
            "search_keyword_competitor_products",
            "sync_tk_influencer_pool",
            "tiktok_fastmoss_product_ingest",
        ]
    }
