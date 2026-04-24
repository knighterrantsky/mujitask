from __future__ import annotations

from automation_business_scaffold.control_plane.runtime_config.settings import (
    INFLUENCER_POOL_TASK_CODE,
    KEYWORD_TASK_CODE,
    PRODUCT_INGEST_TASK_CODE,
    REFRESH_TASK_CODE,
)
from automation_business_scaffold.control_plane.executor.runner import _resolve_workflow_runtime
from automation_business_scaffold.control_plane.executor.workflow_registry import load_workflow_runtime


def test_product_ingest_runtime_can_resolve_to_module_or_safe_fallback() -> None:
    runtime = _resolve_workflow_runtime(PRODUCT_INGEST_TASK_CODE)
    assert runtime is not None
    for entrypoint in (
        "advance_stage",
        "finalize_request",
        "release_request_after_child_completion",
    ):
        assert hasattr(runtime, entrypoint)


def test_unknown_runtime_module_returns_none() -> None:
    assert load_workflow_runtime("unknown_runtime_task") is None


def test_formal_runtime_modules_are_loadable_through_registry() -> None:
    for task_code in (
        PRODUCT_INGEST_TASK_CODE,
        REFRESH_TASK_CODE,
        INFLUENCER_POOL_TASK_CODE,
        KEYWORD_TASK_CODE,
    ):
        runtime = load_workflow_runtime(task_code)
        assert runtime is not None
        for entrypoint in (
            "advance_stage",
            "finalize_request",
            "release_request_after_child_completion",
        ):
            assert hasattr(runtime, entrypoint)
