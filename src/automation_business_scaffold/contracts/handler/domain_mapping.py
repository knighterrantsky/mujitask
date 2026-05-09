from __future__ import annotations

from importlib import import_module
from typing import Any, Mapping


def adapt_source_rows(
    adapter_code: str,
    raw_rows: list[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
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
    registry = import_module("automation_business_scaffold.domains.tiktok.projections.registry")
    return registry.map_projection_record(mapper_code, record, payload)
