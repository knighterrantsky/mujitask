from __future__ import annotations

from types import MappingProxyType

from automation_business_scaffold.capabilities.channels.outbox.message_dispatch_handler import (
    outbox_dispatch_handler,
)

from .allowlist import OUTBOX_HANDLER_CODES, OUTBOX_HANDLER_CONTRACTS
from .contract import HandlerCallable
from .registry import HandlerRegistry, RegisteredHandler

BOUND_OUTBOX_HANDLERS = MappingProxyType({"outbox_dispatch": outbox_dispatch_handler})


def build_outbox_handler_registry() -> HandlerRegistry:
    return HandlerRegistry(
        registry_name="outbox",
        worker_type="outbox_dispatcher",
        runtime_table="notification_outbox",
        allowed_contracts=OUTBOX_HANDLER_CONTRACTS,
    )


def register_outbox_placeholder(
    registry: HandlerRegistry,
    handler_code: str,
) -> RegisteredHandler:
    return registry.register_placeholder(handler_code)


def register_outbox_handler(
    registry: HandlerRegistry,
    handler_code: str,
    handler: HandlerCallable,
    *,
    replace: bool = False,
) -> RegisteredHandler:
    return registry.bind(handler_code, handler, replace=replace)


def bind_default_outbox_handlers(
    registry: HandlerRegistry,
    *,
    replace: bool = False,
) -> HandlerRegistry:
    for handler_code, handler in BOUND_OUTBOX_HANDLERS.items():
        register_outbox_handler(registry, handler_code, handler, replace=replace)
    return registry


def build_bound_outbox_handler_registry(*, replace: bool = False) -> HandlerRegistry:
    registry = build_outbox_handler_registry()
    return bind_default_outbox_handlers(registry, replace=replace)


__all__ = [
    "BOUND_OUTBOX_HANDLERS",
    "OUTBOX_HANDLER_CODES",
    "bind_default_outbox_handlers",
    "build_bound_outbox_handler_registry",
    "build_outbox_handler_registry",
    "register_outbox_handler",
    "register_outbox_placeholder",
]
