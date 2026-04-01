from automation_business_scaffold.registry import build_task_registry


def test_build_task_registry_registers_demo_task():
    registry = build_task_registry()

    assert registry.names() == [
        "source_to_target_publish_demo",
        "tiktok_feishu_batch_sync",
        "tiktok_feishu_single_sync",
        "tiktok_product_link_cleanup",
        "tiktok_product_to_feishu",
    ]
    assert registry.get("source_to_target_publish_demo") is not None
    assert registry.get("tiktok_product_to_feishu") is not None
    assert registry.get("tiktok_feishu_single_sync") is not None
    assert registry.get("tiktok_feishu_batch_sync") is not None
    assert registry.get("tiktok_product_link_cleanup") is not None
