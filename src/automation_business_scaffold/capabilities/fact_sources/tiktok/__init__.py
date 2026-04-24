"""TikTok fact source capabilities."""

from .product_request_fetch_handler import (
    CONTRACT as PRODUCT_REQUEST_FETCH_CONTRACT,
    HANDLER_CODE as PRODUCT_REQUEST_FETCH_HANDLER_CODE,
    tiktok_product_request_fetch_handler,
)

__all__ = [
    "PRODUCT_REQUEST_FETCH_CONTRACT",
    "PRODUCT_REQUEST_FETCH_HANDLER_CODE",
    "tiktok_product_request_fetch_handler",
]
