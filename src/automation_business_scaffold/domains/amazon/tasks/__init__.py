from .refresh_amazon_product_row_by_asin import (
    RefreshAmazonProductRowByAsinTask,
)
from .refresh_current_amazon_product_table import (
    RefreshCurrentAmazonProductTableTask,
)


DEFAULT_TASKS = [
    RefreshAmazonProductRowByAsinTask(),
    RefreshCurrentAmazonProductTableTask(),
]


__all__ = [
    "DEFAULT_TASKS",
    "RefreshAmazonProductRowByAsinTask",
    "RefreshCurrentAmazonProductTableTask",
]
