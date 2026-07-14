from .refresh_amazon_product_row_by_asin import (
    RefreshAmazonProductRowByAsinTask,
)


DEFAULT_TASKS = [RefreshAmazonProductRowByAsinTask()]


__all__ = ["DEFAULT_TASKS", "RefreshAmazonProductRowByAsinTask"]
