from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from types import MappingProxyType
from typing import Any, Mapping, Protocol


_AMAZON_SOURCE_ADAPTER_CODES = {
    "amazon_product_batch_source_adapter",
    "amazon_product_table_source_adapter",
}
_AMAZON_PROJECTION_MAPPER_CODES = {"amazon_product_projection_mapper"}
_RUNTIME_RESULT_PROJECTION_REGISTRIES = MappingProxyType(
    {
        "amazon_product_browser_fetch": (
            "automation_business_scaffold.domains.amazon.projections.registry"
        ),
        "amazon_product_row_persist": (
            "automation_business_scaffold.domains.amazon.projections.registry"
        ),
    }
)


@dataclass(frozen=True)
class RuntimeStorageProjection:
    summary: dict[str, Any]
    result: dict[str, Any]
    artifact_run_id: str = ""
    artifact_records: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class RuntimeFailureProjection:
    summary: dict[str, Any]
    result: dict[str, Any]
    error_text: str
    error_type: str
    error_code: str
    dead_letter_reason: str = ""
    force_terminal: bool = False
    terminal: bool = False


class RuntimeResultProjection(Protocol):
    def project_storage(self, outcome: Any) -> RuntimeStorageProjection: ...

    def projection_failure(
        self,
        outcome: Any,
        error: Exception,
        *,
        phase: str,
    ) -> RuntimeFailureProjection: ...

    def failure_policy(self, outcome: Any) -> RuntimeFailureProjection: ...

    def project_progress(
        self,
        handler_code: str,
        progress_stage: Any,
        message: Any,
    ) -> tuple[str, str]: ...

    def project_response(
        self,
        handler_code: str,
        summary: Any,
        result: Any,
        error_type: Any,
        error_code: Any,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]: ...


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
        registry = import_module("automation_business_scaffold.domains.amazon.projections.registry")
        return registry.map_projection_record(mapper_code, record, payload)
    registry = import_module("automation_business_scaffold.domains.tiktok.projections.registry")
    return registry.map_projection_record(mapper_code, record, payload)


def get_runtime_result_projection(handler_code: str) -> RuntimeResultProjection | None:
    registry_module = _RUNTIME_RESULT_PROJECTION_REGISTRIES.get(handler_code)
    if registry_module is None:
        return None
    registry = import_module(registry_module)
    return registry.get_runtime_result_projection(handler_code)
