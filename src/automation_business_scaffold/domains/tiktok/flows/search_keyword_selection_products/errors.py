from __future__ import annotations

from typing import Any, Mapping


def finalization_error(
    *,
    error_code: str,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "error_code": error_code,
        "reason": reason,
    }
    payload.update(dict(details or {}))
    return payload
