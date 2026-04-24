from __future__ import annotations

from ..allowlist import API_HANDLER_CONTRACTS
from .implementations import media_asset_sync_handler

HANDLER_CODE = "media_asset_sync"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]

__all__ = ["CONTRACT", "HANDLER_CODE", "media_asset_sync_handler"]
