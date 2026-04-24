"""Object storage media asset sync capability facade."""

from automation_business_scaffold.capabilities.media.asset_sync_handler import (
    CONTRACT,
    HANDLER_CODE,
    media_asset_sync_handler,
)

__all__ = ["CONTRACT", "HANDLER_CODE", "media_asset_sync_handler"]
