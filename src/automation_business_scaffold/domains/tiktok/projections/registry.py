from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from automation_business_scaffold.domains.tiktok.projections.feishu_competitor_projection import (
    competitor_influencer_status_projection_mapper,
    competitor_seed_projection_mapper,
    competitor_table_projection_mapper,
)
from automation_business_scaffold.domains.tiktok.projections.feishu_influencer_projection import (
    influencer_pool_projection_mapper,
)
from automation_business_scaffold.domains.tiktok.projections.feishu_outreach_projection import (
    outreach_result_projection_mapper,
)
from automation_business_scaffold.domains.tiktok.projections.feishu_selection_projection import (
    selection_seed_projection_mapper,
    selection_table_projection_mapper,
    selection_writeback_records,
)


@dataclass(frozen=True)
class FeishuProjectionMapperError(Exception):
    error_type: str
    error_code: str
    message: str
    retryable: bool
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)


ProjectionMapper = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]

PROJECTION_MAPPERS = MappingProxyType(
    {
        "competitor_seed_projection_mapper": competitor_seed_projection_mapper,
        "competitor_table_projection_mapper": competitor_table_projection_mapper,
        "influencer_pool_projection_mapper": influencer_pool_projection_mapper,
        "competitor_influencer_status_projection_mapper": competitor_influencer_status_projection_mapper,
        "outreach_result_projection_mapper": outreach_result_projection_mapper,
        "selection_seed_projection_mapper": selection_seed_projection_mapper,
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
        raise FeishuProjectionMapperError(
            error_type="configuration_error",
            error_code="unsupported_mapper",
            message=f"Unsupported Feishu projection mapper: {normalized_mapper_code}",
            retryable=False,
            details={"mapper_code": normalized_mapper_code},
        )
    return mapper(record, payload)


__all__ = [
    "FeishuProjectionMapperError",
    "PROJECTION_MAPPER_CODES",
    "PROJECTION_MAPPERS",
    "ProjectionMapper",
    "get_projection_mapper",
    "map_projection_record",
    "selection_writeback_records",
]
