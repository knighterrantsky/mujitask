from __future__ import annotations

from ..allowlist import BROWSER_HANDLER_CONTRACTS
from .implementations import tiktok_product_browser_fetch_handler

HANDLER_CODE = "tiktok_product_browser_fetch"
CONTRACT = BROWSER_HANDLER_CONTRACTS[HANDLER_CODE]

__all__ = ["CONTRACT", "HANDLER_CODE", "tiktok_product_browser_fetch_handler"]
