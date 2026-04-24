from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

import automation_business_scaffold.business.feishu_common as _common

SourceAdapter = Callable[[list[Mapping[str, Any]], Mapping[str, Any]], dict[str, Any]]


def competitor_table_source_adapter(
    raw_rows: list[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _common._adapt_competitor_rows(raw_rows, payload)


def influencer_pool_source_adapter(
    raw_rows: list[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _common._adapt_influencer_source_rows(raw_rows, payload)


def selection_table_source_adapter(
    raw_rows: list[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _common._adapt_selection_rows(raw_rows, payload)


SOURCE_ADAPTERS = MappingProxyType(
    {
        "competitor_table_source_adapter": competitor_table_source_adapter,
        "influencer_pool_source_adapter": influencer_pool_source_adapter,
        "selection_table_source_adapter": selection_table_source_adapter,
    }
)
SOURCE_ADAPTER_CODES = frozenset(SOURCE_ADAPTERS)


def get_source_adapter(adapter_code: str) -> SourceAdapter | None:
    return SOURCE_ADAPTERS.get(adapter_code)


def adapt_source_rows(
    adapter_code: str,
    raw_rows: list[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    adapter = get_source_adapter(adapter_code)
    if adapter is None:
        raise _common.FeishuCommonError(
            error_type="configuration_error",
            error_code="unsupported_adapter",
            message=f"Unsupported Feishu source adapter: {adapter_code}",
            retryable=False,
            details={"adapter_code": adapter_code},
        )
    return adapter(raw_rows, payload)


__all__ = [
    "SOURCE_ADAPTER_CODES",
    "SOURCE_ADAPTERS",
    "SourceAdapter",
    "adapt_source_rows",
    "competitor_table_source_adapter",
    "get_source_adapter",
    "influencer_pool_source_adapter",
    "selection_table_source_adapter",
]
