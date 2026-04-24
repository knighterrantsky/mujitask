"""TikTok browser product fetch capability facade."""

from automation_business_scaffold.business.handlers.allowlist import BROWSER_HANDLER_CONTRACTS
from automation_business_scaffold.capabilities.browser.implementations import tiktok_product_browser_fetch_handler

HANDLER_CODE = "tiktok_product_browser_fetch"
CONTRACT = BROWSER_HANDLER_CONTRACTS[HANDLER_CODE]

__all__ = ["CONTRACT", "HANDLER_CODE", "tiktok_product_browser_fetch_handler"]
