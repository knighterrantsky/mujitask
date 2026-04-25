from .refresh_competitor_row_by_url import RefreshCompetitorRowByUrlTask
from .refresh_current_competitor_table import RefreshCurrentCompetitorTableTask
from .search_keyword_competitor_products import SearchKeywordCompetitorProductsTask
from .sync_tk_influencer_pool import SyncTKInfluencerPoolTask
from .tiktok_fastmoss_product_ingest import TikTokFastMossProductIngestTask

DEFAULT_TASKS = [
    RefreshCurrentCompetitorTableTask(),
    RefreshCompetitorRowByUrlTask(),
    SearchKeywordCompetitorProductsTask(),
    SyncTKInfluencerPoolTask(),
    TikTokFastMossProductIngestTask(),
]

__all__ = [
    "DEFAULT_TASKS",
    "RefreshCompetitorRowByUrlTask",
    "RefreshCurrentCompetitorTableTask",
    "SearchKeywordCompetitorProductsTask",
    "SyncTKInfluencerPoolTask",
    "TikTokFastMossProductIngestTask",
]
