from __future__ import annotations

from ..allowlist import OUTBOX_HANDLER_CONTRACTS
from .implementations import outbox_dispatch_handler

HANDLER_CODE = "outbox_dispatch"
CONTRACT = OUTBOX_HANDLER_CONTRACTS[HANDLER_CODE]

__all__ = ["CONTRACT", "HANDLER_CODE", "outbox_dispatch_handler"]
