from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.shared import first_non_empty


def fastmoss_browser_fallback_payload(
    *,
    request_payload: Mapping[str, Any],
    payload: Mapping[str, Any],
    fastmoss_result: Mapping[str, Any],
    source_record_id: str,
    identity: Mapping[str, Any],
    fallback_source_job_id: str,
) -> dict[str, Any]:
    return {
        **dict(request_payload),
        **dict(payload),
        **dict(fastmoss_result),
        "request_payload": dict(request_payload),
        "source_record_id": source_record_id,
        "product_identity": dict(identity),
        "fallback_source_job_id": first_non_empty(
            fastmoss_result.get("fallback_source_job_id"), fallback_source_job_id
        ),
    }
