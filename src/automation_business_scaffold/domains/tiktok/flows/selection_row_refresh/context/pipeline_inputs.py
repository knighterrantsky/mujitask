from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.shared import coerce_mapping


def request_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    nested = coerce_mapping(payload.get("request_payload"))
    if nested:
        return nested
    return dict(payload)


def source_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    nested = coerce_mapping(payload.get("source_context"))
    if nested:
        return nested
    return dict(payload)


def source_fields(source_context: Mapping[str, Any]) -> dict[str, Any]:
    for candidate in (
        source_context.get("source_fields"),
        source_context.get("fields"),
        coerce_mapping(source_context.get("source_context")).get("source_fields"),
        coerce_mapping(source_context.get("source_context")).get("fields"),
    ):
        if isinstance(candidate, Mapping):
            return dict(candidate)
    return {}


def first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None
