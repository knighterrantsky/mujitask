from __future__ import annotations

from types import MappingProxyType

from ..allowlist import OUTBOX_HANDLER_CONTRACTS
from ..contract import HandlerCallable
from ..registry import HandlerRegistry, RegisteredHandler
from .implementations import outbox_dispatch_handler

BOUND_OUTBOX_HANDLERS = MappingProxyType({"outbox_dispatch": outbox_dispatch_handler})
OUTBOX_HANDLER_CODES = frozenset(OUTBOX_HANDLER_CONTRACTS)


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
