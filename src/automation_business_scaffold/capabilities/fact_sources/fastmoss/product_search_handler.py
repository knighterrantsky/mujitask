"""FastMoss product search capability facade."""

from automation_business_scaffold.business.handlers.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.capabilities._implementations.api import fastmoss_product_search_handler

HANDLER_CODE = "fastmoss_product_search"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]

__all__ = ["CONTRACT", "HANDLER_CODE", "fastmoss_product_search_handler"]
