from __future__ import annotations

import json
from typing import Any, Mapping


def json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def coerce_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def coerce_non_negative_float(value: Any) -> float:
    return max(coerce_float(value), 0.0)


def coerce_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def load_json_dict(raw_value: str | None) -> dict[str, Any]:
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_json_list(raw_value: str | None) -> list[dict[str, Any]]:
    if not raw_value:
        return []
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [dict(item) for item in payload if isinstance(item, Mapping)]


def resolve_runtime_seconds(
    row_value: Any,
    payload: Mapping[str, Any],
    *payload_keys: str,
    default: float = 0.0,
) -> float:
    row_seconds = coerce_non_negative_float(row_value)
    if row_seconds > 0:
        return row_seconds
    for key in payload_keys:
        payload_seconds = coerce_non_negative_float(payload.get(key))
        if payload_seconds > 0:
            return payload_seconds
    return coerce_non_negative_float(default)


def build_bind_placeholders(prefix: str, values: tuple[str, ...]) -> tuple[str, dict[str, Any]]:
    placeholders: list[str] = []
    params: dict[str, Any] = {}
    for index, value in enumerate(values):
        key = f"{prefix}_{index}"
        placeholders.append(f":{key}")
        params[key] = value
    return ", ".join(placeholders), params
