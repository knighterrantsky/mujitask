from automation_business_scaffold.registry import build_task_registry


def test_build_task_registry_registers_demo_task():
    registry = build_task_registry()

    assert registry.names() == [
        "fastmoss_keyword_candidate_discovery",
        "fastmoss_login_check",
        "fastmoss_product_sales_snapshot",
        "feishu_clear_row_by_url",
        "feishu_pending_rows_scan",
        "feishu_seed_row_insert",
        "feishu_single_row_update",
        "refresh_current_competitor_table",
        "source_to_target_publish_demo",
        "tiktok_feishu_single_sync",
        "tiktok_product_link_cleanup",
        "tiktok_product_to_feishu",
    ]
    assert registry.get("fastmoss_login_check") is not None
    assert registry.get("fastmoss_keyword_candidate_discovery") is not None
    assert registry.get("source_to_target_publish_demo") is not None
    assert registry.get("feishu_clear_row_by_url") is not None
    assert registry.get("feishu_pending_rows_scan") is not None
    assert registry.get("feishu_seed_row_insert") is not None
    assert registry.get("feishu_single_row_update") is not None
    assert registry.get("tiktok_product_to_feishu") is not None
    assert registry.get("tiktok_feishu_single_sync") is not None
    assert registry.get("tiktok_product_link_cleanup") is not None
    assert registry.get("fastmoss_product_sales_snapshot") is not None
