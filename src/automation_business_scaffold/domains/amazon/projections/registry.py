from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from .feishu_product_projection import amazon_product_projection_mapper
from .runtime_result_projection import AMAZON_RUNTIME_RESULT_PROJECTION


ProjectionMapper = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]

PROJECTION_MAPPERS = MappingProxyType(
    {"amazon_product_projection_mapper": amazon_product_projection_mapper}
)
PROJECTION_MAPPER_CODES = frozenset(PROJECTION_MAPPERS)
RUNTIME_RESULT_PROJECTIONS = MappingProxyType(
    {
        "amazon_product_browser_fetch": AMAZON_RUNTIME_RESULT_PROJECTION,
        "amazon_product_row_persist": AMAZON_RUNTIME_RESULT_PROJECTION,
    }
)


def map_projection_record(
    mapper_code: str,
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    mapper = PROJECTION_MAPPERS.get(mapper_code)
    if mapper is None:
        raise ValueError(f"Unsupported Amazon Feishu projection mapper: {mapper_code}")
    return mapper(record, payload)


def get_runtime_result_projection(handler_code: str) -> Any | None:
    return RUNTIME_RESULT_PROJECTIONS.get(handler_code)


__all__ = [
    "PROJECTION_MAPPER_CODES",
    "PROJECTION_MAPPERS",
    "RUNTIME_RESULT_PROJECTIONS",
    "ProjectionMapper",
    "get_runtime_result_projection",
    "map_projection_record",
]
