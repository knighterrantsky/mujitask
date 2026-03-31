from automation_business_scaffold.tasks.source_to_target_publish_demo import (
    SourceToTargetPublishDemoTask,
)
from automation_business_scaffold.tasks.tiktok_feishu_batch_sync import (
    TikTokFeishuBatchSyncTask,
)
from automation_business_scaffold.tasks.tiktok_feishu_single_sync import (
    TikTokFeishuSingleSyncTask,
)
from automation_business_scaffold.tasks.tiktok_product_to_feishu import (
    TikTokProductToFeishuTask,
)

DEFAULT_TASKS = [
    SourceToTargetPublishDemoTask(),
    TikTokProductToFeishuTask(),
    TikTokFeishuSingleSyncTask(),
    TikTokFeishuBatchSyncTask(),
]

__all__ = [
    "DEFAULT_TASKS",
    "SourceToTargetPublishDemoTask",
    "TikTokProductToFeishuTask",
    "TikTokFeishuSingleSyncTask",
    "TikTokFeishuBatchSyncTask",
]
