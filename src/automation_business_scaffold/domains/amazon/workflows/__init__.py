from automation_business_scaffold.contracts.workflow import WorkflowDefinition

from .refresh_amazon_product_row_by_asin import (
    REFRESH_AMAZON_PRODUCT_ROW_BY_ASIN_DEFINITION,
    build_refresh_amazon_product_row_by_asin_definition,
    build_refresh_amazon_product_row_by_asin_workflow,
)


DEFAULT_WORKFLOW_DEFINITIONS = (REFRESH_AMAZON_PRODUCT_ROW_BY_ASIN_DEFINITION,)


def list_workflow_definitions() -> tuple[WorkflowDefinition, ...]:
    return DEFAULT_WORKFLOW_DEFINITIONS


def get_workflow_definition(task_code: str) -> WorkflowDefinition:
    for workflow in DEFAULT_WORKFLOW_DEFINITIONS:
        if workflow.task_code == task_code:
            return workflow
    raise KeyError(task_code)


__all__ = [
    "DEFAULT_WORKFLOW_DEFINITIONS",
    "REFRESH_AMAZON_PRODUCT_ROW_BY_ASIN_DEFINITION",
    "build_refresh_amazon_product_row_by_asin_definition",
    "build_refresh_amazon_product_row_by_asin_workflow",
    "get_workflow_definition",
    "list_workflow_definitions",
]
