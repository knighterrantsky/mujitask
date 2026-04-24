"""Fact bundle upsert persistence capability facade."""

from automation_business_scaffold.business.handlers.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.capabilities._implementations.api import fact_bundle_upsert_handler

HANDLER_CODE = "fact_bundle_upsert"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]

__all__ = ["CONTRACT", "HANDLER_CODE", "fact_bundle_upsert_handler"]
