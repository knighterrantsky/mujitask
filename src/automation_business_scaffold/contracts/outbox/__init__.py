"""Outbox handler contracts and registry entry points."""

from automation_business_scaffold.contracts.handler.allowlist import OUTBOX_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.outbox import (
    BOUND_OUTBOX_HANDLERS,
    OUTBOX_HANDLER_CODES,
    bind_default_outbox_handlers,
    build_bound_outbox_handler_registry,
    build_outbox_handler_registry,
    register_outbox_handler,
    register_outbox_placeholder,
)

__all__ = [
    "BOUND_OUTBOX_HANDLERS",
    "OUTBOX_HANDLER_CODES",
    "OUTBOX_HANDLER_CONTRACTS",
    "bind_default_outbox_handlers",
    "build_bound_outbox_handler_registry",
    "build_outbox_handler_registry",
    "register_outbox_handler",
    "register_outbox_placeholder",
]
