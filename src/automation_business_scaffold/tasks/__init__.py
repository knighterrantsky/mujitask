from automation_business_scaffold.tasks.source_to_target_publish_demo import (
    SourceToTargetPublishDemoTask,
)
from automation_business_scaffold.tasks.tiktok_product_to_feishu import (
    TikTokProductToFeishuTask,
)

DEFAULT_TASKS = [SourceToTargetPublishDemoTask(), TikTokProductToFeishuTask()]

__all__ = ["DEFAULT_TASKS", "SourceToTargetPublishDemoTask", "TikTokProductToFeishuTask"]
