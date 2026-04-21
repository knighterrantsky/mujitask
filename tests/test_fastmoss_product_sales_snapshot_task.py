from __future__ import annotations

from types import SimpleNamespace

from automation_business_scaffold.models import FastMossProductSalesSnapshot
from automation_business_scaffold.business.tasks.fastmoss_product_sales_snapshot import (
    FastMossProductSalesSnapshotTask,
)


def test_fastmoss_product_sales_snapshot_task_defaults_verify_login_to_false(monkeypatch):
    module = __import__(
        "automation_business_scaffold.business.tasks.fastmoss_product_sales_snapshot",
        fromlist=["FastMossProductSalesSnapshotTask"],
    )

    captured: dict[str, object] = {}

    def fake_fetch(product_id, **kwargs):
        captured["product_id"] = product_id
        captured.update(kwargs)
        return FastMossProductSalesSnapshot(
            product_id=str(product_id),
            search_url="https://www.fastmoss.com/zh/e-commerce/search?page=1&words=1732268173492064949",
            detail_url=f"https://www.fastmoss.com/zh/e-commerce/detail/{product_id}",
            product_title="Sample Product",
            login_state="skipped_login_verification",
            fastmoss_price_amount="29.91",
            yesterday_sales="1",
            sales_7d="2",
            sales_28d="3",
            sales_90d="4",
        )

    monkeypatch.setattr(module, "fetch_fastmoss_product_sales_via_browser", fake_fetch)

    task = FastMossProductSalesSnapshotTask()
    result = task.execute_workflow_step(
        SimpleNamespace(
            step=SimpleNamespace(step_id="fetch_fastmoss_sales_snapshot"),
            params={
                "product_id": "1732268173492064949",
            },
        )
    )

    assert captured["product_id"] == "1732268173492064949"
    assert captured["verify_login"] is False
    assert result.data["fastmoss_snapshot"]["login_state"] == "skipped_login_verification"
