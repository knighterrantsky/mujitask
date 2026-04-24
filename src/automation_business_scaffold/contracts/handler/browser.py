from __future__ import annotations

from types import MappingProxyType

from automation_business_scaffold.capabilities.browser.tiktok_product_fetch_handler import (
    tiktok_product_browser_fetch_handler,
)

from .allowlist import BROWSER_HANDLER_CODES, BROWSER_HANDLER_CONTRACTS
from .contract import HandlerCallable
from .registry import HandlerRegistry, RegisteredHandler

BOUND_BROWSER_HANDLERS = MappingProxyType(
    {"tiktok_product_browser_fetch": tiktok_product_browser_fetch_handler}
)


def build_browser_handler_registry() -> HandlerRegistry:
    return HandlerRegistry(
        registry_name="browser",
        worker_type="browser_worker",
        runtime_table="task_execution",
        allowed_contracts=BROWSER_HANDLER_CONTRACTS,
    )


def register_browser_placeholder(
    registry: HandlerRegistry,
    handler_code: str,
) -> RegisteredHandler:
    return registry.register_placeholder(handler_code)


def register_browser_handler(
    registry: HandlerRegistry,
    handler_code: str,
    handler: HandlerCallable,
    *,
    replace: bool = False,
) -> RegisteredHandler:
    return registry.bind(handler_code, handler, replace=replace)


def bind_default_browser_handlers(
    registry: HandlerRegistry,
    *,
    replace: bool = False,
) -> HandlerRegistry:
    for handler_code, handler in BOUND_BROWSER_HANDLERS.items():
        register_browser_handler(registry, handler_code, handler, replace=replace)
    return registry


def build_bound_browser_handler_registry(*, replace: bool = False) -> HandlerRegistry:
    registry = build_browser_handler_registry()
    return bind_default_browser_handlers(registry, replace=replace)


__all__ = [
    "BROWSER_HANDLER_CODES",
    "BOUND_BROWSER_HANDLERS",
    "bind_default_browser_handlers",
    "build_browser_handler_registry",
    "build_bound_browser_handler_registry",
    "register_browser_handler",
    "register_browser_placeholder",
]
