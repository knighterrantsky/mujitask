from .amazon_product_browser_fetch import AMAZON_PRODUCT_BROWSER_FETCH_JOB
from .amazon_product_fact_upsert import AMAZON_PRODUCT_FACT_UPSERT_JOB
from .amazon_product_row_persist import AMAZON_PRODUCT_ROW_PERSIST_JOB
from .amazon_product_row_refresh import AMAZON_PRODUCT_ROW_REFRESH_JOB
from .feishu_table_read import FEISHU_TABLE_READ_JOB
from .feishu_table_write import FEISHU_TABLE_WRITE_JOB
from .task_completed_notification import TASK_COMPLETED_NOTIFICATION_JOB


def list_job_definitions():
    return (
        FEISHU_TABLE_READ_JOB,
        AMAZON_PRODUCT_BROWSER_FETCH_JOB,
        AMAZON_PRODUCT_FACT_UPSERT_JOB,
        AMAZON_PRODUCT_ROW_PERSIST_JOB,
        AMAZON_PRODUCT_ROW_REFRESH_JOB,
        FEISHU_TABLE_WRITE_JOB,
        TASK_COMPLETED_NOTIFICATION_JOB,
    )


__all__ = [
    "AMAZON_PRODUCT_BROWSER_FETCH_JOB",
    "AMAZON_PRODUCT_FACT_UPSERT_JOB",
    "AMAZON_PRODUCT_ROW_PERSIST_JOB",
    "AMAZON_PRODUCT_ROW_REFRESH_JOB",
    "FEISHU_TABLE_READ_JOB",
    "FEISHU_TABLE_WRITE_JOB",
    "TASK_COMPLETED_NOTIFICATION_JOB",
    "list_job_definitions",
]
