from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from automation_business_scaffold.domains.tiktok.mappers.feishu_competitor_row_mapper import (
    competitor_table_source_adapter,
)
from automation_business_scaffold.domains.tiktok.mappers.feishu_influencer_source_mapper import (
    influencer_pool_source_adapter,
)
from automation_business_scaffold.domains.tiktok.mappers.feishu_outreach_source_mapper import (
    outreach_source_adapter,
)
from automation_business_scaffold.domains.tiktok.mappers.feishu_selection_row_mapper import (
    selection_table_source_adapter,
)


@dataclass(frozen=True)
class FeishuSourceAdapterError(Exception):
    error_type: str
    error_code: str
    message: str
    retryable: bool
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)


SourceAdapter = Callable[[list[Mapping[str, Any]], Mapping[str, Any]], dict[str, Any]]

SOURCE_ADAPTERS = MappingProxyType(
    {
        "competitor_table_source_adapter": competitor_table_source_adapter,
        "influencer_pool_source_adapter": influencer_pool_source_adapter,
        "outreach_source_adapter": outreach_source_adapter,
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
        raise FeishuSourceAdapterError(
            error_type="configuration_error",
            error_code="unsupported_adapter",
            message=f"Unsupported Feishu source adapter: {adapter_code}",
            retryable=False,
            details={"adapter_code": adapter_code},
        )
    return adapter(raw_rows, payload)


__all__ = [
    "FeishuSourceAdapterError",
    "SOURCE_ADAPTER_CODES",
    "SOURCE_ADAPTERS",
    "SourceAdapter",
    "adapt_source_rows",
    "get_source_adapter",
]
