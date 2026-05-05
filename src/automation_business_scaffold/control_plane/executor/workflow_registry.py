from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any, Protocol

from automation_business_scaffold.contracts.workflow import WorkflowDefinition
from automation_business_scaffold.control_plane.runtime_config.settings import (
    INFLUENCER_POOL_TASK_CODE,
    KEYWORD_TASK_CODE,
    PRODUCT_INGEST_TASK_CODE,
    SELECTION_KEYWORD_TASK_CODE,
    REFRESH_COMPETITOR_ROW_BY_URL_TASK_CODE,
    REFRESH_TASK_CODE,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


class WorkflowRuntimeModule(Protocol):
    def advance_stage(
        self,
        *,
        store: RuntimeStore,
        request: Any,
        workflow: WorkflowDefinition,
        stage_code: str,
    ) -> dict[str, Any]: ...

    def finalize_request(
        self,
        *,
        store: RuntimeStore,
        request: Any,
        workflow: WorkflowDefinition,
        force_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def release_request_after_child_completion(
        self,
        store: RuntimeStore,
        *,
        request_id: str,
    ) -> list[dict[str, Any]]: ...


WORKFLOW_RUNTIME_MODULES = {
    PRODUCT_INGEST_TASK_CODE: (
        "automation_business_scaffold.domains.tiktok.flows."
        "tiktok_fastmoss_product_ingest"
    ),
    REFRESH_TASK_CODE: (
        "automation_business_scaffold.domains.tiktok.flows."
        "refresh_current_competitor_table"
    ),
    REFRESH_COMPETITOR_ROW_BY_URL_TASK_CODE: (
        "automation_business_scaffold.domains.tiktok.flows."
        "refresh_current_competitor_table"
    ),
    INFLUENCER_POOL_TASK_CODE: (
        "automation_business_scaffold.domains.tiktok.flows.sync_tk_influencer_pool"
    ),
    KEYWORD_TASK_CODE: (
        "automation_business_scaffold.domains.tiktok.flows."
        "search_keyword_competitor_products"
    ),
    SELECTION_KEYWORD_TASK_CODE: (
        "automation_business_scaffold.domains.tiktok.flows."
        "search_keyword_selection_products"
    ),
}


def load_workflow_runtime(task_code: str) -> WorkflowRuntimeModule | None:
    module_name = WORKFLOW_RUNTIME_MODULES.get(str(task_code or "").strip())
    if not module_name:
        return None
    try:
        module = import_module(module_name)
    except ModuleNotFoundError:
        return None
    return _validate_runtime_module(module)


def _validate_runtime_module(module: ModuleType) -> WorkflowRuntimeModule | None:
    required = (
        "advance_stage",
        "finalize_request",
        "release_request_after_child_completion",
    )
    if not all(hasattr(module, name) for name in required):
        return None
    return module  # type: ignore[return-value]


__all__ = [
    "WORKFLOW_RUNTIME_MODULES",
    "WorkflowRuntimeModule",
    "load_workflow_runtime",
]
