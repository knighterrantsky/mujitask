from __future__ import annotations

from ..allowlist import API_HANDLER_CONTRACTS
from .implementations import fact_bundle_upsert_handler

HANDLER_CODE = "fact_bundle_upsert"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]

__all__ = ["CONTRACT", "HANDLER_CODE", "fact_bundle_upsert_handler"]
