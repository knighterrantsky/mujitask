from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from .feishu_product_projection import amazon_product_projection_mapper


ProjectionMapper = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]

PROJECTION_MAPPERS = MappingProxyType(
    {"amazon_product_projection_mapper": amazon_product_projection_mapper}
)
PROJECTION_MAPPER_CODES = frozenset(PROJECTION_MAPPERS)


def map_projection_record(
    mapper_code: str,
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    mapper = PROJECTION_MAPPERS.get(mapper_code)
    if mapper is None:
        raise ValueError(f"Unsupported Amazon Feishu projection mapper: {mapper_code}")
    return mapper(record, payload)


__all__ = [
    "PROJECTION_MAPPER_CODES",
    "PROJECTION_MAPPERS",
    "ProjectionMapper",
    "map_projection_record",
]
