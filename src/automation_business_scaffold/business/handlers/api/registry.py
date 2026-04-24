from __future__ import annotations

from types import MappingProxyType

from ..allowlist import API_HANDLER_CODES as API_HANDLER_CODES, API_HANDLER_CONTRACTS
from ..contract import HandlerCallable
from ..registry import HandlerRegistry, RegisteredHandler
from .implementations import (
    fact_bundle_upsert_handler,
    feishu_table_read_handler,
    feishu_table_write_handler,
    fastmoss_creator_fetch_handler,
    fastmoss_product_fetch_handler,
    fastmoss_product_search_handler,
    media_asset_sync_handler,
    tiktok_product_request_fetch_handler,
)

BOUND_API_HANDLERS = MappingProxyType(
    {
        "feishu_table_read": feishu_table_read_handler,
        "feishu_table_write": feishu_table_write_handler,
        "tiktok_product_request_fetch": tiktok_product_request_fetch_handler,
        "fastmoss_product_search": fastmoss_product_search_handler,
        "fastmoss_product_fetch": fastmoss_product_fetch_handler,
        "fastmoss_creator_fetch": fastmoss_creator_fetch_handler,
        "media_asset_sync": media_asset_sync_handler,
        "fact_bundle_upsert": fact_bundle_upsert_handler,
    }
)


def build_api_handler_registry() -> HandlerRegistry:
    return HandlerRegistry(
        registry_name="api",
        worker_type="api_worker",
        runtime_table="api_worker_job",
        allowed_contracts=API_HANDLER_CONTRACTS,
    )


def register_api_placeholder(
    registry: HandlerRegistry,
    handler_code: str,
) -> RegisteredHandler:
    return registry.register_placeholder(handler_code)


def register_api_handler(
    registry: HandlerRegistry,
    handler_code: str,
    handler: HandlerCallable,
    *,
    replace: bool = False,
) -> RegisteredHandler:
    return registry.bind(handler_code, handler, replace=replace)


def bind_default_api_handlers(
    registry: HandlerRegistry,
    *,
    replace: bool = False,
) -> HandlerRegistry:
    for handler_code, handler in BOUND_API_HANDLERS.items():
        register_api_handler(registry, handler_code, handler, replace=replace)
    return registry


def build_bound_api_handler_registry(*, replace: bool = False) -> HandlerRegistry:
    registry = build_api_handler_registry()
    return bind_default_api_handlers(registry, replace=replace)
