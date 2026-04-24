from __future__ import annotations

from ..allowlist import API_HANDLER_CONTRACTS
from .implementations import fastmoss_creator_fetch_handler

HANDLER_CODE = "fastmoss_creator_fetch"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]

__all__ = ["CONTRACT", "HANDLER_CODE", "fastmoss_creator_fetch_handler"]
