from automation_business_scaffold.contracts.workflow import WorkflowDefinition
from .refresh_current_competitor_table import (
    REFRESH_CURRENT_COMPETITOR_TABLE_DEFINITION,
    build_refresh_current_competitor_table_definition,
    build_refresh_current_competitor_table_workflow,
)
from .search_keyword_competitor_products import (
    SEARCH_KEYWORD_COMPETITOR_PRODUCTS_DEFINITION,
    build_search_keyword_competitor_products_definition,
    build_search_keyword_competitor_products_workflow,
)
from .sync_tk_influencer_pool import (
    SYNC_TK_INFLUENCER_POOL_DEFINITION,
    build_sync_tk_influencer_pool_definition,
    build_sync_tk_influencer_pool_workflow,
)
from .tiktok_fastmoss_product_ingest import (
    TIKTOK_FASTMOSS_PRODUCT_INGEST_DEFINITION,
    build_tiktok_fastmoss_product_ingest_definition,
    build_tiktok_fastmoss_product_ingest_workflow,
)

DEFAULT_WORKFLOW_DEFINITIONS = (
    REFRESH_CURRENT_COMPETITOR_TABLE_DEFINITION,
    SEARCH_KEYWORD_COMPETITOR_PRODUCTS_DEFINITION,
    SYNC_TK_INFLUENCER_POOL_DEFINITION,
    TIKTOK_FASTMOSS_PRODUCT_INGEST_DEFINITION,
)


def list_workflow_definitions() -> tuple[WorkflowDefinition, ...]:
    return DEFAULT_WORKFLOW_DEFINITIONS


def get_workflow_definition(task_code: str) -> WorkflowDefinition:
    for workflow in DEFAULT_WORKFLOW_DEFINITIONS:
        if workflow.task_code == task_code:
            return workflow
    raise KeyError(task_code)


def get_workflow_definition_by_code(workflow_code: str) -> WorkflowDefinition:
    for workflow in DEFAULT_WORKFLOW_DEFINITIONS:
        if workflow.workflow_code == workflow_code:
            return workflow
    raise KeyError(workflow_code)

__all__ = [
    "DEFAULT_WORKFLOW_DEFINITIONS",
    "REFRESH_CURRENT_COMPETITOR_TABLE_DEFINITION",
    "SEARCH_KEYWORD_COMPETITOR_PRODUCTS_DEFINITION",
    "SYNC_TK_INFLUENCER_POOL_DEFINITION",
    "TIKTOK_FASTMOSS_PRODUCT_INGEST_DEFINITION",
    "WorkflowDefinition",
    "build_refresh_current_competitor_table_definition",
    "build_refresh_current_competitor_table_workflow",
    "build_search_keyword_competitor_products_definition",
    "build_search_keyword_competitor_products_workflow",
    "build_sync_tk_influencer_pool_definition",
    "build_sync_tk_influencer_pool_workflow",
    "build_tiktok_fastmoss_product_ingest_definition",
    "build_tiktok_fastmoss_product_ingest_workflow",
    "get_workflow_definition",
    "get_workflow_definition_by_code",
    "list_workflow_definitions",
]
