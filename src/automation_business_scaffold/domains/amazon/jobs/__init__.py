from .amazon_product_browser_fetch import AMAZON_PRODUCT_BROWSER_FETCH_JOB
from .amazon_product_fact_upsert import AMAZON_PRODUCT_FACT_UPSERT_JOB
from .amazon_product_row_persist import AMAZON_PRODUCT_ROW_PERSIST_JOB


def list_job_definitions():
    return (
        AMAZON_PRODUCT_BROWSER_FETCH_JOB,
        AMAZON_PRODUCT_FACT_UPSERT_JOB,
        AMAZON_PRODUCT_ROW_PERSIST_JOB,
    )


__all__ = [
    "AMAZON_PRODUCT_BROWSER_FETCH_JOB",
    "AMAZON_PRODUCT_FACT_UPSERT_JOB",
    "AMAZON_PRODUCT_ROW_PERSIST_JOB",
    "list_job_definitions",
]
