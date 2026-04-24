"""FastMoss video fetch capability contract facade."""

from automation_business_scaffold.business.handlers.allowlist import API_HANDLER_CONTRACTS

HANDLER_CODE = "fastmoss_video_fetch"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]

__all__ = ["CONTRACT", "HANDLER_CODE"]
