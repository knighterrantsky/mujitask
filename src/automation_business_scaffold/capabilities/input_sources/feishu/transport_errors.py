from __future__ import annotations

import requests

from automation_business_scaffold.capabilities.input_sources.feishu.targets import (
    FeishuCommonError,
)
from automation_business_scaffold.infrastructure.feishu.api import FeishuAPIError


def classify_feishu_exception(exc: Exception) -> FeishuCommonError:
    if isinstance(exc, FeishuCommonError):
        return exc
    if all(hasattr(exc, name) for name in ("error_type", "error_code", "message", "retryable")):
        return FeishuCommonError(
            error_type=str(getattr(exc, "error_type")),
            error_code=str(getattr(exc, "error_code")),
            message=str(getattr(exc, "message")),
            retryable=bool(getattr(exc, "retryable")),
            details=dict(getattr(exc, "details") or {}),
        )
    if isinstance(exc, FeishuAPIError):
        message = str(exc)
        status = int(exc.status or 0)
        code = int(exc.code or 0)
        lowered = message.lower()
        details = {"status": exc.status, "code": exc.code}
        if status in {401, 403} or code in {99991663, 99991664}:
            return FeishuCommonError("auth_error", "feishu_auth_error", message, False, details)
        if status == 429 or code in {1254290, 99991400} or "rate" in lowered:
            return FeishuCommonError("rate_limited", "feishu_rate_limited", message, True, details)
        if status in {408, 504} or "timeout" in lowered or "timed out" in lowered:
            return FeishuCommonError("timeout", "feishu_timeout", message, True, details)
        if "field" in lowered or "schema" in lowered or "not exist" in lowered or "not found" in lowered:
            return FeishuCommonError("schema_missing", "feishu_schema_missing", message, False, details)
        if status >= 500 or status == 0:
            return FeishuCommonError("upstream_error", "feishu_upstream_error", message, True, details)
        return FeishuCommonError("upstream_error", "feishu_api_error", message, False, details)
    if isinstance(exc, (requests.exceptions.Timeout, TimeoutError)):
        return FeishuCommonError("timeout", "feishu_timeout", str(exc), True, {})
    if isinstance(exc, requests.exceptions.RequestException):
        return FeishuCommonError("upstream_error", "feishu_transport_error", str(exc), True, {})
    return FeishuCommonError("upstream_error", "feishu_unexpected_error", str(exc), True, {})
