from __future__ import annotations

from automation_business_scaffold.contracts.workflow import JobDefinition
from automation_business_scaffold.domains.tiktok.jobs.competitor_row_refresh import COMPETITOR_ROW_REFRESH_JOB
from automation_business_scaffold.domains.tiktok.jobs.fact_bundle_upsert import FACT_BUNDLE_UPSERT_JOB
from automation_business_scaffold.domains.tiktok.jobs.fastmoss_creator_fetch import FASTMOSS_CREATOR_FETCH_JOB
from automation_business_scaffold.domains.tiktok.jobs.fastmoss_product_fetch import FASTMOSS_PRODUCT_FETCH_JOB
from automation_business_scaffold.domains.tiktok.jobs.fastmoss_product_search import FASTMOSS_PRODUCT_SEARCH_JOB
from automation_business_scaffold.domains.tiktok.jobs.feishu_table_read import FEISHU_TABLE_READ_JOB
from automation_business_scaffold.domains.tiktok.jobs.feishu_table_write import FEISHU_TABLE_WRITE_JOB
from automation_business_scaffold.domains.tiktok.jobs.media_asset_sync import MEDIA_ASSET_SYNC_JOB
from automation_business_scaffold.domains.tiktok.jobs.task_completed_notification import TASK_COMPLETED_NOTIFICATION_JOB
from automation_business_scaffold.domains.tiktok.jobs.tiktok_product_browser_fetch import TIKTOK_PRODUCT_BROWSER_FETCH_JOB
from automation_business_scaffold.domains.tiktok.jobs.tiktok_product_request_fetch import TIKTOK_PRODUCT_REQUEST_FETCH_JOB


def list_job_definitions() -> tuple[JobDefinition, ...]:
    return (
        FEISHU_TABLE_READ_JOB,
        FEISHU_TABLE_WRITE_JOB,
        COMPETITOR_ROW_REFRESH_JOB,
        TIKTOK_PRODUCT_REQUEST_FETCH_JOB,
        TIKTOK_PRODUCT_BROWSER_FETCH_JOB,
        FASTMOSS_PRODUCT_SEARCH_JOB,
        FASTMOSS_PRODUCT_FETCH_JOB,
        FASTMOSS_CREATOR_FETCH_JOB,
        MEDIA_ASSET_SYNC_JOB,
        FACT_BUNDLE_UPSERT_JOB,
        TASK_COMPLETED_NOTIFICATION_JOB,
    )


__all__ = [
    "COMPETITOR_ROW_REFRESH_JOB",
    "FACT_BUNDLE_UPSERT_JOB",
    "FASTMOSS_CREATOR_FETCH_JOB",
    "FASTMOSS_PRODUCT_FETCH_JOB",
    "FASTMOSS_PRODUCT_SEARCH_JOB",
    "FEISHU_TABLE_READ_JOB",
    "FEISHU_TABLE_WRITE_JOB",
    "MEDIA_ASSET_SYNC_JOB",
    "TASK_COMPLETED_NOTIFICATION_JOB",
    "TIKTOK_PRODUCT_BROWSER_FETCH_JOB",
    "TIKTOK_PRODUCT_REQUEST_FETCH_JOB",
    "list_job_definitions",
]
