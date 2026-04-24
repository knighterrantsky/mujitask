from __future__ import annotations

from collections.abc import Iterable

from .models import WorkflowDefinition
from .refresh_current_competitor_table import REFRESH_CURRENT_COMPETITOR_TABLE_DEFINITION
from .search_keyword_competitor_products import SEARCH_KEYWORD_COMPETITOR_PRODUCTS_DEFINITION
from .sync_tk_influencer_pool import SYNC_TK_INFLUENCER_POOL_DEFINITION
from .tiktok_fastmoss_product_ingest import TIKTOK_FASTMOSS_PRODUCT_INGEST_DEFINITION

DEFAULT_WORKFLOW_DEFINITIONS: tuple[WorkflowDefinition, ...] = (
    REFRESH_CURRENT_COMPETITOR_TABLE_DEFINITION,
    SEARCH_KEYWORD_COMPETITOR_PRODUCTS_DEFINITION,
    SYNC_TK_INFLUENCER_POOL_DEFINITION,
    TIKTOK_FASTMOSS_PRODUCT_INGEST_DEFINITION,
)


class WorkflowRegistry:
    def __init__(self, definitions: Iterable[WorkflowDefinition] = ()) -> None:
        self._by_task_code: dict[str, WorkflowDefinition] = {}
        self._by_workflow_code: dict[str, WorkflowDefinition] = {}
        self.register_many(definitions)

    def register(self, definition: WorkflowDefinition) -> None:
        existing_task = self._by_task_code.get(definition.task_code)
        if existing_task is not None and existing_task is not definition:
            raise ValueError(f"Duplicate workflow task_code: {definition.task_code}")

        existing_workflow = self._by_workflow_code.get(definition.workflow_code)
        if existing_workflow is not None and existing_workflow is not definition:
            raise ValueError(f"Duplicate workflow workflow_code: {definition.workflow_code}")

        self._by_task_code[definition.task_code] = definition
        self._by_workflow_code[definition.workflow_code] = definition

    def register_many(self, definitions: Iterable[WorkflowDefinition]) -> None:
        for definition in definitions:
            self.register(definition)

    def get(self, task_code: str) -> WorkflowDefinition | None:
        return self._by_task_code.get(task_code)

    def require(self, task_code: str) -> WorkflowDefinition:
        definition = self.get(task_code)
        if definition is None:
            raise KeyError(f"Unknown workflow task_code: {task_code}")
        return definition

    def get_by_workflow_code(self, workflow_code: str) -> WorkflowDefinition | None:
        return self._by_workflow_code.get(workflow_code)

    def require_by_workflow_code(self, workflow_code: str) -> WorkflowDefinition:
        definition = self.get_by_workflow_code(workflow_code)
        if definition is None:
            raise KeyError(f"Unknown workflow workflow_code: {workflow_code}")
        return definition

    def all(self) -> tuple[WorkflowDefinition, ...]:
        return tuple(self._by_task_code.values())

    def task_codes(self) -> tuple[str, ...]:
        return tuple(self._by_task_code.keys())

    def workflow_codes(self) -> tuple[str, ...]:
        return tuple(self._by_workflow_code.keys())


def build_workflow_registry(
    definitions: Iterable[WorkflowDefinition] = DEFAULT_WORKFLOW_DEFINITIONS,
) -> WorkflowRegistry:
    return WorkflowRegistry(definitions)


WORKFLOW_REGISTRY = build_workflow_registry()


def list_workflow_definitions() -> tuple[WorkflowDefinition, ...]:
    return WORKFLOW_REGISTRY.all()


def get_workflow_definition(task_code: str) -> WorkflowDefinition:
    return WORKFLOW_REGISTRY.require(task_code)


def get_workflow_definition_by_code(workflow_code: str) -> WorkflowDefinition:
    return WORKFLOW_REGISTRY.require_by_workflow_code(workflow_code)
