from __future__ import annotations

from types import MappingProxyType

from automation_business_scaffold.capabilities.channels.feishu.table_write_handler import (
    feishu_table_write_handler,
)
from automation_business_scaffold.capabilities.fact_sources.fastmoss.creator_fetch_handler import (
    fastmoss_creator_fetch_handler,
)
from automation_business_scaffold.capabilities.fact_sources.fastmoss.product_fetch_handler import (
    fastmoss_product_fetch_handler,
)
from automation_business_scaffold.capabilities.fact_sources.fastmoss.product_search_handler import (
    fastmoss_product_search_handler,
)
from automation_business_scaffold.capabilities.fact_sources.fastmoss.shop_fetch_handler import (
    fastmoss_shop_fetch_handler,
)
from automation_business_scaffold.capabilities.fact_sources.fastmoss.video_fetch_handler import (
    fastmoss_video_fetch_handler,
)
from automation_business_scaffold.capabilities.fact_sources.tiktok.competitor_row_refresh_handler import (
    competitor_row_refresh_handler,
)
from automation_business_scaffold.capabilities.fact_sources.tiktok.product_request_fetch_handler import (
    tiktok_product_request_fetch_handler,
)
from automation_business_scaffold.capabilities.input_sources.feishu.table_read_handler import (
    feishu_table_read_handler,
)
from automation_business_scaffold.capabilities.media.asset_sync_handler import media_asset_sync_handler
from automation_business_scaffold.capabilities.persistence.database.fact_bundle_upsert_handler import (
    fact_bundle_upsert_handler,
)

from .allowlist import API_HANDLER_CODES, API_HANDLER_CONTRACTS
from .contract import HandlerCallable
from .registry import HandlerRegistry, RegisteredHandler

BOUND_API_HANDLERS = MappingProxyType(
    {
        "feishu_table_read": feishu_table_read_handler,
        "feishu_table_write": feishu_table_write_handler,
        "competitor_row_refresh": competitor_row_refresh_handler,
        "tiktok_product_request_fetch": tiktok_product_request_fetch_handler,
        "fastmoss_product_search": fastmoss_product_search_handler,
        "fastmoss_product_fetch": fastmoss_product_fetch_handler,
        "fastmoss_creator_fetch": fastmoss_creator_fetch_handler,
        "fastmoss_shop_fetch": fastmoss_shop_fetch_handler,
        "fastmoss_video_fetch": fastmoss_video_fetch_handler,
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


__all__ = [
    "API_HANDLER_CODES",
    "BOUND_API_HANDLERS",
    "bind_default_api_handlers",
    "build_api_handler_registry",
    "build_bound_api_handler_registry",
    "register_api_handler",
    "register_api_placeholder",
]
