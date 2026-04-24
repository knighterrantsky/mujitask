# Platform-managed: keep the registry shell stable; task ownership is domain-local.

from __future__ import annotations

from automation_framework.core import TaskRegistry

from automation_business_scaffold.domains.tiktok.tasks import DEFAULT_TASKS


def build_task_registry() -> TaskRegistry:
    registry = TaskRegistry()
    registry.register_many(DEFAULT_TASKS)
    return registry
