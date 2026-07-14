from __future__ import annotations

from importlib import import_module
from typing import Final

from automation_business_scaffold.contracts.handler.contract import (
    HandlerCallable,
    HandlerContext,
    HandlerResult,
)


_API_HANDLER_EXPORTS: Final[dict[str, tuple[str, str]]] = {
    "feishu_table_read": (
        "automation_business_scaffold.capabilities.input_sources.feishu.table_read_handler",
        "feishu_table_read_handler",
    ),
    "feishu_table_write": (
        "automation_business_scaffold.capabilities.channels.feishu.table_write_handler",
        "feishu_table_write_handler",
    ),
    "competitor_row_refresh": (
        "automation_business_scaffold.domains.tiktok.jobs.competitor_row_refresh",
        "competitor_row_refresh_handler",
    ),
    "keyword_seed_import": (
        "automation_business_scaffold.domains.tiktok.jobs.keyword_seed_import",
        "keyword_seed_import_handler",
    ),
    "product_creator_discovery": (
        "automation_business_scaffold.domains.tiktok.jobs.product_creator_discovery",
        "product_creator_discovery_handler",
    ),
    "influencer_creator_sync": (
        "automation_business_scaffold.domains.tiktok.jobs.influencer_creator_sync",
        "influencer_creator_sync_handler",
    ),
    "tiktok_product_request_fetch": (
        "automation_business_scaffold.capabilities.fact_sources.tiktok.product_request_fetch_handler",
        "tiktok_product_request_fetch_handler",
    ),
    "fastmoss_product_search": (
        "automation_business_scaffold.capabilities.fact_sources.fastmoss.product_search_handler",
        "fastmoss_product_search_handler",
    ),
    "fastmoss_product_fetch": (
        "automation_business_scaffold.capabilities.fact_sources.fastmoss.product_fetch_handler",
        "fastmoss_product_fetch_handler",
    ),
    "fastmoss_creator_fetch": (
        "automation_business_scaffold.capabilities.fact_sources.fastmoss.creator_fetch_handler",
        "fastmoss_creator_fetch_handler",
    ),
    "fastmoss_shop_fetch": (
        "automation_business_scaffold.capabilities.fact_sources.fastmoss.shop_fetch_handler",
        "fastmoss_shop_fetch_handler",
    ),
    "fastmoss_video_fetch": (
        "automation_business_scaffold.capabilities.fact_sources.fastmoss.video_fetch_handler",
        "fastmoss_video_fetch_handler",
    ),
    "media_asset_sync": (
        "automation_business_scaffold.capabilities.media.asset_sync_handler",
        "media_asset_sync_handler",
    ),
    "fact_bundle_upsert": (
        "automation_business_scaffold.capabilities.persistence.database.fact_bundle_upsert_handler",
        "fact_bundle_upsert_handler",
    ),
    "amazon_product_fact_upsert": (
        "automation_business_scaffold.capabilities.persistence.database.amazon_product_fact_upsert_handler",
        "amazon_product_fact_upsert_handler",
    ),
    "amazon_product_row_persist": (
        "automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist",
        "amazon_product_row_persist_handler",
    ),
    "selection_row_refresh": (
        "automation_business_scaffold.domains.tiktok.jobs.selection_row_refresh",
        "selection_row_refresh_handler",
    ),
}


def get_api_handler(handler_code: str) -> HandlerCallable:
    try:
        module_name, export_name = _API_HANDLER_EXPORTS[handler_code]
    except KeyError as exc:
        raise ValueError(f"Unknown API handler code: {handler_code}") from exc
    handler = getattr(import_module(module_name), export_name)
    return handler


def api_handler_callable(handler_code: str) -> HandlerCallable:
    def dispatch(context: HandlerContext) -> HandlerResult:
        return get_api_handler(handler_code)(context)

    return dispatch


__all__ = ["api_handler_callable", "get_api_handler"]
