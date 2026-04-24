from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

import automation_business_scaffold.business.feishu_common as _common

ProjectionMapper = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]


def competitor_seed_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _common._map_competitor_seed_record(record, payload)


def competitor_table_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _common._map_competitor_table_record(record, payload)


def influencer_pool_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _common._map_influencer_pool_record(record, payload)


def competitor_influencer_status_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _common._map_competitor_influencer_status_record(record, payload)


def selection_table_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _common._map_selection_table_record(record, payload)


PROJECTION_MAPPERS = MappingProxyType(
    {
        "competitor_seed_projection_mapper": competitor_seed_projection_mapper,
        "competitor_table_projection_mapper": competitor_table_projection_mapper,
        "influencer_pool_projection_mapper": influencer_pool_projection_mapper,
        "competitor_influencer_status_projection_mapper": competitor_influencer_status_projection_mapper,
        "selection_table_projection_mapper": selection_table_projection_mapper,
    }
)
PROJECTION_MAPPER_CODES = frozenset(PROJECTION_MAPPERS)


def get_projection_mapper(mapper_code: str) -> ProjectionMapper | None:
    return PROJECTION_MAPPERS.get(mapper_code)


def map_projection_record(
    mapper_code: str,
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_mapper_code = mapper_code or "selection_table_projection_mapper"
    mapper = get_projection_mapper(normalized_mapper_code)
    if mapper is None:
        raise _common.FeishuCommonError(
            error_type="configuration_error",
            error_code="unsupported_mapper",
            message=f"Unsupported Feishu projection mapper: {normalized_mapper_code}",
            retryable=False,
            details={"mapper_code": normalized_mapper_code},
        )
    return mapper(record, payload)


def selection_writeback_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _common._selection_writeback_records(payload)


__all__ = [
    "PROJECTION_MAPPER_CODES",
    "PROJECTION_MAPPERS",
    "ProjectionMapper",
    "competitor_influencer_status_projection_mapper",
    "competitor_seed_projection_mapper",
    "competitor_table_projection_mapper",
    "get_projection_mapper",
    "influencer_pool_projection_mapper",
    "map_projection_record",
    "selection_table_projection_mapper",
    "selection_writeback_records",
]
