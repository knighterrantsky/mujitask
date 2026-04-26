"""FastMoss fact source capabilities."""

from .creator_fetch_handler import (
    CONTRACT as CREATOR_FETCH_CONTRACT,
    HANDLER_CODE as CREATOR_FETCH_HANDLER_CODE,
    fastmoss_creator_fetch_handler,
)
from .product_fetch_handler import (
    CONTRACT as PRODUCT_FETCH_CONTRACT,
    HANDLER_CODE as PRODUCT_FETCH_HANDLER_CODE,
    fastmoss_product_fetch_handler,
)
from .product_search_handler import (
    CONTRACT as PRODUCT_SEARCH_CONTRACT,
    HANDLER_CODE as PRODUCT_SEARCH_HANDLER_CODE,
    fastmoss_product_search_handler,
)
from .shop_fetch_handler import (
    CONTRACT as SHOP_FETCH_CONTRACT,
    HANDLER_CODE as SHOP_FETCH_HANDLER_CODE,
    fastmoss_shop_fetch_handler,
)
from .video_fetch_handler import (
    CONTRACT as VIDEO_FETCH_CONTRACT,
    HANDLER_CODE as VIDEO_FETCH_HANDLER_CODE,
    fastmoss_video_fetch_handler,
)

__all__ = [
    "CREATOR_FETCH_CONTRACT",
    "CREATOR_FETCH_HANDLER_CODE",
    "PRODUCT_FETCH_CONTRACT",
    "PRODUCT_FETCH_HANDLER_CODE",
    "PRODUCT_SEARCH_CONTRACT",
    "PRODUCT_SEARCH_HANDLER_CODE",
    "SHOP_FETCH_CONTRACT",
    "SHOP_FETCH_HANDLER_CODE",
    "VIDEO_FETCH_CONTRACT",
    "VIDEO_FETCH_HANDLER_CODE",
    "fastmoss_creator_fetch_handler",
    "fastmoss_product_fetch_handler",
    "fastmoss_product_search_handler",
    "fastmoss_shop_fetch_handler",
    "fastmoss_video_fetch_handler",
]
