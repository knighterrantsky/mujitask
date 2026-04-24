from __future__ import annotations

from ..allowlist import API_HANDLER_CONTRACTS

HANDLER_CODE = "fastmoss_shop_fetch"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]

__all__ = ["CONTRACT", "HANDLER_CODE"]
