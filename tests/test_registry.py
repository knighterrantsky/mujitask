from automation_business_scaffold.registry import build_task_registry


def test_build_task_registry_registers_demo_task():
    registry = build_task_registry()

    assert registry.names() == ["source_to_target_publish_demo"]
    assert registry.get("source_to_target_publish_demo") is not None

