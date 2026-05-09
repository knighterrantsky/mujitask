from __future__ import annotations

import os
import re
from typing import Any, Mapping

from automation_business_scaffold.capabilities.input_sources.feishu.batch_write import (
    execute_write_records,
)
from automation_business_scaffold.capabilities.input_sources.feishu.row_reading import (
    read_feishu_records,
)
from automation_business_scaffold.capabilities.input_sources.feishu.schema_normalization import (
    normalize_raw_rows,
    validate_read_schema,
    validate_write_schema,
)
from automation_business_scaffold.capabilities.input_sources.feishu.targets import (
    FeishuCommonError,
    FeishuTableTarget,
)
from automation_business_scaffold.capabilities.input_sources.feishu.transport_errors import (
    classify_feishu_exception,
)
from automation_business_scaffold.capabilities.input_sources.feishu.write_payloads import (
    map_write_records,
)
from automation_business_scaffold.infrastructure.feishu.api import (
    FeishuBitableClient,
    parse_table_url,
)
from automation_business_scaffold.infrastructure.rate_limit import RequestPacer, resolve_api_request_pacer_config


def build_feishu_client(
    target: FeishuTableTarget,
    settings: Mapping[str, Any] | None = None,
) -> FeishuBitableClient:
    request_pacer = RequestPacer(resolve_api_request_pacer_config(settings, provider="feishu"))
    try:
        return FeishuBitableClient(target.access_token, request_pacer=request_pacer)
    except TypeError as exc:
        if "request_pacer" not in str(exc):
            raise
        return FeishuBitableClient(target.access_token)


def resolve_read_target(payload: Mapping[str, Any]) -> FeishuTableTarget:
    return _resolve_table_target(payload, table_ref_key="source_table_ref")


def resolve_write_target(payload: Mapping[str, Any]) -> FeishuTableTarget:
    return _resolve_table_target(payload, table_ref_key="target_table_ref")


def adapt_source_rows(
    raw_rows: list[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    adapter_code = _text(payload.get("adapter_code"))
    if not adapter_code:
        return {
            "source_rows": [],
            "candidate_keys": [],
            "adapter_summary": {
                "adapter_code": "",
                "input_row_count": len(raw_rows),
                "source_row_count": 0,
            },
        }
    from automation_business_scaffold.contracts.handler.domain_mapping import (
        adapt_source_rows as run_source_adapter,
    )

    return run_source_adapter(adapter_code, raw_rows, payload)


def _resolve_table_target(payload: Mapping[str, Any], *, table_ref_key: str) -> FeishuTableTarget:
    request_payload = _mapping(payload.get("request_payload"))
    table_ref = _text(payload.get(table_ref_key))
    table_payload = _resolve_table_payload(payload, request_payload, table_ref=table_ref)
    table_ref_url = table_ref if table_ref.startswith(("http://", "https://")) else ""
    table_url = _first_non_empty(
        table_payload.get("table_url"),
        table_ref_url,
        payload.get("source_table_url" if table_ref_key == "source_table_ref" else "target_table_url"),
        request_payload.get("source_table_url" if table_ref_key == "source_table_ref" else "target_table_url"),
        payload.get("table_url"),
        request_payload.get("table_url"),
    )

    parsed: dict[str, Any] = {}
    if table_url:
        try:
            parsed = dict(parse_table_url(table_url))
        except ValueError as exc:
            raise FeishuCommonError(
                error_type="configuration_error",
                error_code="invalid_table_url",
                message=str(exc),
                retryable=False,
                details={"table_url": table_url, "table_ref": table_ref},
            ) from exc

    app_token = _first_non_empty(
        table_payload.get("app_token"),
        table_payload.get("app_token_ref") if not _looks_like_secret_ref(table_payload.get("app_token_ref")) else "",
        parsed.get("app_token"),
        _resolve_secret_ref(table_payload.get("app_token_ref")),
    )
    table_id = _first_non_empty(table_payload.get("table_id"), parsed.get("table_id"))
    view_id = _first_non_empty(table_payload.get("view_id"), payload.get("view_id"), payload.get("view_ref"), parsed.get("view_id"))
    access_token = _resolve_access_token(payload, request_payload, table_payload)

    missing = []
    if not access_token:
        missing.append("access_token")
    if not app_token:
        missing.append("app_token")
    if not table_id:
        missing.append("table_id")
    if missing:
        raise FeishuCommonError(
            error_type="configuration_error",
            error_code="missing_feishu_table_target",
            message="Feishu table target could not be resolved.",
            retryable=False,
            details={"missing": missing, "table_ref": table_ref, "table_url": table_url},
        )

    return FeishuTableTarget(
        access_token=access_token,
        app_token=app_token,
        table_id=table_id,
        view_id=view_id,
        table_ref=table_ref,
        table_url=table_url,
    )


def _resolve_table_payload(
    payload: Mapping[str, Any],
    request_payload: Mapping[str, Any],
    *,
    table_ref: str,
) -> dict[str, Any]:
    table_payload = _mapping(payload.get("feishu_table"))
    if table_payload:
        return table_payload
    table_refs = _mapping(payload.get("table_refs")) or _mapping(request_payload.get("table_refs"))
    resolved = table_refs.get(table_ref)
    if isinstance(resolved, Mapping):
        return dict(resolved)
    if isinstance(resolved, str):
        return {"table_url": resolved}
    return {}


def _resolve_access_token(
    payload: Mapping[str, Any],
    request_payload: Mapping[str, Any],
    table_payload: Mapping[str, Any],
) -> str:
    access_token_env = _first_non_empty(
        table_payload.get("access_token_env"),
        payload.get("access_token_env"),
        request_payload.get("access_token_env"),
    )
    return _first_non_empty(
        table_payload.get("access_token"),
        payload.get("access_token"),
        payload.get("feishu_access_token"),
        request_payload.get("access_token"),
        request_payload.get("feishu_access_token"),
        os.environ.get(access_token_env, "") if access_token_env else "",
        _resolve_secret_ref(table_payload.get("access_token_ref")),
        os.environ.get("MUJITASK_FEISHU_ACCESS_TOKEN", ""),
    )


def _resolve_secret_ref(value: Any) -> str:
    item = _text(value)
    if not item:
        return ""
    if item.startswith("env://"):
        return os.environ.get(item.removeprefix("env://"), "")
    if item.startswith("secret://"):
        suffix = re.sub(r"[^A-Za-z0-9]+", "_", item.rsplit("/", 1)[-1]).strip("_").upper()
        for env_name in (f"FEISHU_{suffix}", f"MUJITASK_FEISHU_{suffix}"):
            candidate = os.environ.get(env_name, "")
            if candidate:
                return candidate
    if item in os.environ:
        return os.environ.get(item, "")
    return ""


def _looks_like_secret_ref(value: Any) -> bool:
    item = _text(value)
    return item.startswith(("secret://", "env://"))


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _first_non_empty(*values: Any) -> str:
    for value in values:
        item = _text(value)
        if item:
            return item
    return ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


__all__ = [
    "FeishuBitableClient",
    "FeishuCommonError",
    "FeishuTableTarget",
    "adapt_source_rows",
    "build_feishu_client",
    "classify_feishu_exception",
    "execute_write_records",
    "map_write_records",
    "normalize_raw_rows",
    "read_feishu_records",
    "resolve_read_target",
    "resolve_write_target",
    "validate_read_schema",
    "validate_write_schema",
]
