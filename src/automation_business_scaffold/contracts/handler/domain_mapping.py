from __future__ import annotations

from importlib import import_module
from typing import Any, Mapping


_AMAZON_SOURCE_ADAPTER_CODES = {"amazon_product_table_source_adapter"}
_AMAZON_PROJECTION_MAPPER_CODES = {"amazon_product_projection_mapper"}


def adapt_source_rows(
    adapter_code: str,
    raw_rows: list[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if adapter_code in _AMAZON_SOURCE_ADAPTER_CODES:
        registry = import_module("automation_business_scaffold.domains.amazon.mappers.registry")
        return registry.adapt_source_rows(adapter_code, raw_rows, payload)
    registry = import_module("automation_business_scaffold.domains.tiktok.mappers.registry")
    return registry.adapt_source_rows(adapter_code, raw_rows, payload)


def selection_writeback_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    registry = import_module("automation_business_scaffold.domains.tiktok.projections.registry")
    return registry.selection_writeback_records(payload)


def map_projection_record(
    mapper_code: str,
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if mapper_code in _AMAZON_PROJECTION_MAPPER_CODES:
        registry = import_module(
            "automation_business_scaffold.domains.amazon.projections.registry"
        )
        return registry.map_projection_record(mapper_code, record, payload)
    registry = import_module("automation_business_scaffold.domains.tiktok.projections.registry")
    return registry.map_projection_record(mapper_code, record, payload)
