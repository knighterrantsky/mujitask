"""Media asset sync capability facade."""

from automation_business_scaffold.business.handlers.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.capabilities._implementations.api import media_asset_sync_handler

HANDLER_CODE = "media_asset_sync"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]

__all__ = ["CONTRACT", "HANDLER_CODE", "media_asset_sync_handler"]
