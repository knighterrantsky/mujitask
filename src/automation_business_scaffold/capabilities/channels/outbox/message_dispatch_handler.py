"""Outbox message dispatch channel capability facade."""

from automation_business_scaffold.business.handlers.allowlist import OUTBOX_HANDLER_CONTRACTS
from automation_business_scaffold.capabilities.channels.outbox.implementations import outbox_dispatch_handler

HANDLER_CODE = "outbox_dispatch"
CONTRACT = OUTBOX_HANDLER_CONTRACTS[HANDLER_CODE]

__all__ = ["CONTRACT", "HANDLER_CODE", "outbox_dispatch_handler"]
