from automation_business_scaffold.tasks.fastmoss_login_check import FastMossLoginCheckTask
from automation_business_scaffold.tasks.fastmoss_keyword_candidate_discovery import (
    FastMossKeywordCandidateDiscoveryTask,
)
from automation_business_scaffold.tasks.fastmoss_product_sales_snapshot import (
    FastMossProductSalesSnapshotTask,
)
from automation_business_scaffold.tasks.feishu_pending_rows_scan import FeishuPendingRowsScanTask
from automation_business_scaffold.tasks.feishu_seed_row_insert import FeishuSeedRowInsertTask
from automation_business_scaffold.tasks.feishu_single_row_update import FeishuSingleRowUpdateTask
from automation_business_scaffold.tasks.source_to_target_publish_demo import (
    SourceToTargetPublishDemoTask,
)
from automation_business_scaffold.tasks.tiktok_feishu_single_sync import (
    TikTokFeishuSingleSyncTask,
)
from automation_business_scaffold.tasks.tiktok_product_link_cleanup import (
    TikTokProductLinkCleanupTask,
)
from automation_business_scaffold.tasks.tiktok_product_to_feishu import (
    TikTokProductToFeishuTask,
)

DEFAULT_TASKS = [
    SourceToTargetPublishDemoTask(),
    FastMossLoginCheckTask(),
    FeishuPendingRowsScanTask(),
    FeishuSeedRowInsertTask(),
    FeishuSingleRowUpdateTask(),
    FastMossKeywordCandidateDiscoveryTask(),
    TikTokProductToFeishuTask(),
    TikTokFeishuSingleSyncTask(),
    TikTokProductLinkCleanupTask(),
    FastMossProductSalesSnapshotTask(),
]

__all__ = [
    "DEFAULT_TASKS",
    "FastMossLoginCheckTask",
    "FastMossKeywordCandidateDiscoveryTask",
    "FastMossProductSalesSnapshotTask",
    "FeishuPendingRowsScanTask",
    "FeishuSeedRowInsertTask",
    "FeishuSingleRowUpdateTask",
    "SourceToTargetPublishDemoTask",
    "TikTokProductToFeishuTask",
    "TikTokFeishuSingleSyncTask",
    "TikTokProductLinkCleanupTask",
]
