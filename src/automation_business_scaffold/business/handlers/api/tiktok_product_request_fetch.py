from __future__ import annotations

from ..allowlist import API_HANDLER_CONTRACTS
from .implementations import tiktok_product_request_fetch_handler

HANDLER_CODE = "tiktok_product_request_fetch"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]

__all__ = ["CONTRACT", "HANDLER_CODE", "tiktok_product_request_fetch_handler"]
