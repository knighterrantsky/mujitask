from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from .feishu_product_source_mapper import (
    amazon_product_batch_source_adapter,
    amazon_product_table_source_adapter,
)


SourceAdapter = Callable[[list[Mapping[str, Any]], Mapping[str, Any]], dict[str, Any]]

SOURCE_ADAPTERS = MappingProxyType(
    {
        "amazon_product_batch_source_adapter": amazon_product_batch_source_adapter,
        "amazon_product_table_source_adapter": amazon_product_table_source_adapter,
    }
)
SOURCE_ADAPTER_CODES = frozenset(SOURCE_ADAPTERS)


def adapt_source_rows(
    adapter_code: str,
    raw_rows: list[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    adapter = SOURCE_ADAPTERS.get(adapter_code)
    if adapter is None:
        raise ValueError(f"Unsupported Amazon Feishu source adapter: {adapter_code}")
    return adapter(raw_rows, payload)


__all__ = [
    "SOURCE_ADAPTER_CODES",
    "SOURCE_ADAPTERS",
    "SourceAdapter",
    "adapt_source_rows",
]
