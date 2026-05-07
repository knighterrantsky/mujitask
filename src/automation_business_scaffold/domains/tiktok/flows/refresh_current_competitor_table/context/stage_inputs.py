from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from automation_business_scaffold.contracts.handler.shared import (
    bundle_entity_keys,
    coerce_mapping,
    compact_dict,
    merge_fact_bundles,
)
from automation_business_scaffold.contracts.workflow import WorkflowDefinition
from automation_business_scaffold.contracts.workflow.execution_helpers import (
    all_child_records as _all_child_records,
    any_api_jobs_active as _any_api_jobs_active,
    any_browser_executions_active as _any_browser_executions_active,
    api_jobs_for_stage as _api_jobs_for_stage,
    browser_executions_for_stage as _browser_executions_for_stage,
    build_projection_record,
    build_projection_write_payload,
    build_stage_local_dedupe_key,
    compute_final_status,
    extract_effective_result_payload,
    extract_handler_result_status,
    has_active_records as _has_active_children,
    is_fallback_required,
    recover_browser_fallback_resume_stage,
    render_job_keys,
    select_latest_successful_api_job,
    select_latest_successful_api_job_result,
    stage_child_records as _stage_child_records,
    summarize_child_outcomes,
    summarize_stage_children,
    timeout_seconds_for_workflow as _timeout_seconds,
)
from automation_business_scaffold.domains.tiktok.mappers.keyword_search_mapper import (
    keyword_search_parameter_mapper,
)
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition
from .models import *


def _empty_row_delete_records(read_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_rows = read_payload.get("raw_rows_all") or read_payload.get("raw_rows") or []
    records: list[dict[str, Any]] = []
    for row in raw_rows:
        if not isinstance(row, Mapping):
            continue
        record_id = str(row.get("record_id") or "").strip()
        fields = row.get("fields")
        if record_id and isinstance(fields, Mapping) and not _any_field_has_value(fields):
            records.append(
                {
                    "op": "delete",
                    "record_id": record_id,
                    "business_entity_key": f"empty-row:{record_id}",
                    "source_context": {"cleanup_reason": "empty_row"},
                }
            )
    return records

def _any_field_has_value(fields: Mapping[str, Any]) -> bool:
    return any(_field_has_value(value) for value in fields.values())

def _field_has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_field_has_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_field_has_value(item) for item in value)
    return True

def _browser_execution_payload(
    *,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    fallback_handler = str(candidate.get("fallback_handler") or "")
    fallback_payload = (
        dict(candidate.get("browser_fallback_payload"))
        if isinstance(candidate.get("browser_fallback_payload"), Mapping)
        else {}
    )
    payload = {
        **_runtime_child_context(request=request, workflow=workflow, stage_code=stage_code),
        **_payload_subset(request.payload, ARTIFACT_PASSTHROUGH_KEYS),
        **fallback_payload,
        "stage_code": stage_code,
        "source_record_id": str(candidate.get("source_record_id") or ""),
        "business_entity_key": str(candidate.get("business_entity_key") or ""),
        "fallback_handler": fallback_handler,
        "fallback_source_job_id": _first_text(
            fallback_payload.get("fallback_source_job_id"),
            candidate.get("row_job_id"),
        ),
    }
    payload.update(_artifact_settings_from_request_payload(request.payload))
    if fallback_handler == "fastmoss_security_browser_resolve":
        payload.setdefault("search_query", str(candidate.get("business_entity_key") or ""))
        payload.setdefault("search_digest", _search_digest_for_row_fallback(candidate))
        if not isinstance(payload.get("search_request"), Mapping):
            payload["search_request"] = {}
        if not isinstance(payload.get("verification_request"), Mapping):
            payload["verification_request"] = {}
        fastmoss_settings = _fastmoss_settings_from_request_payload(request.payload)
        if fastmoss_settings:
            payload["fastmoss"] = fastmoss_settings
    return _compact_mapping(payload)

def _resume_row_payload(*, stage_code: str, candidate: Mapping[str, Any]) -> dict[str, Any]:
    fallback_handler = str(candidate.get("fallback_handler") or "")
    payload = dict(candidate.get("row_payload") or {}) if isinstance(candidate.get("row_payload"), Mapping) else {}
    browser_payload = (
        dict(candidate.get("browser_execution_payload"))
        if isinstance(candidate.get("browser_execution_payload"), Mapping)
        else {}
    )
    payload.update(
        {
            "stage_code": stage_code,
            "browser_fallback_resolved": True,
            "browser_fallback_handler": fallback_handler,
            "browser_execution_id": str(candidate.get("browser_execution_id") or ""),
            "fallback_source_job_id": str(candidate.get("row_job_id") or ""),
            "force_fallback": False,
            "fallback_reason": "",
        }
    )
    if fallback_handler == "tiktok_product_browser_fetch":
        normalized = browser_payload.get("normalized_product_result")
        if isinstance(normalized, Mapping):
            payload["normalized_product_result"] = dict(normalized)
    elif fallback_handler == "fastmoss_security_browser_resolve":
        payload["fastmoss_security_browser_fallback_attempt"] = 1
        normalized = candidate.get("normalized_product_result")
        if isinstance(normalized, Mapping) and normalized:
            payload["normalized_product_result"] = dict(normalized)
    return _compact_mapping(payload)

def _row_fallback_key(*, source_record_id: str, fallback_handler: str) -> str:
    return f"{fallback_handler}:{source_record_id}"

def _search_digest_for_row_fallback(candidate: Mapping[str, Any]) -> str:
    value = _first_text(
        candidate.get("source_record_id"),
        candidate.get("business_entity_key"),
        candidate.get("row_job_id"),
    )
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16] if value else ""

def _row_browser_resource_code(
    *,
    fallback_handler: str,
    payload: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> str:
    if fallback_handler == "fastmoss_security_browser_resolve":
        return _fastmoss_browser_resource_code(payload)
    return _browser_resource_code(candidate)

def _build_writeback_projection(
    *,
    store: RuntimeStore,
    request_id: str,
    row_context: Mapping[str, Any],
) -> dict[str, Any]:
    row_result = _build_row_result(store=store, request_id=request_id, row_context=row_context)
    projection_fields = _build_competitor_projection_fields(
        store=store,
        request_id=request_id,
        row_context=row_context,
    )
    status_field = _competitor_status_text(str(row_result["row_status"]))
    if status_field:
        projection_fields["商品状态"] = status_field
    return build_projection_record(
        request_id=request_id,
        source_record_id=str(row_context["source_record_id"]),
        product_id=str(row_context.get("product_id") or row_context["product_identity"].get("product_id") or ""),
        product_url=str(row_context.get("normalized_product_url") or row_context["product_identity"].get("product_url") or ""),
        refresh_status=str(row_result["row_status"]),
        details=row_result,
        candidate_key=str(row_context.get("business_key") or ""),
        extra_fields={
            "business_entity_key": str(row_context.get("business_key") or ""),
            "projection_fields": projection_fields,
            "source_fields": _source_fields_from_row_context(row_context),
        },
    )

def _build_competitor_projection_fields(
    *,
    store: RuntimeStore,
    request_id: str,
    row_context: Mapping[str, Any],
) -> dict[str, Any]:
    source_record_id = str(row_context.get("source_record_id") or "")
    collect_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="collect_product_data")
    media_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="sync_media")
    browser_execs = _browser_executions_for_stage(store=store, request_id=request_id, stage_code="browser_fallback")
    tiktok_job = _latest_row_job(collect_jobs, source_record_id=source_record_id, job_code="tiktok_product_request_fetch")
    fastmoss_job = _latest_row_job(collect_jobs, source_record_id=source_record_id, job_code="fastmoss_product_fetch")
    media_job = _latest_row_job(media_jobs, source_record_id=source_record_id, job_code="media_asset_sync")
    browser_execution = _latest_row_execution(browser_execs, source_record_id=source_record_id)

    tiktok_result = _effective_tiktok_result(tiktok_job=tiktok_job, browser_execution=browser_execution)
    fastmoss_result = extract_effective_result_payload(fastmoss_job)
    media_result = extract_effective_result_payload(media_job)
    product_result = dict(tiktok_result.get("normalized_product_result") or {})
    tiktok_product = dict(product_result.get("product") or {})
    logical_fields = dict(product_result.get("logical_fields") or {})
    fastmoss_bundle = dict(fastmoss_result.get("product_fact_bundle") or {})
    daily_metrics = [
        dict(item)
        for item in fastmoss_bundle.get("product_daily_metrics", [])
        if isinstance(item, Mapping)
    ]
    fastmoss_product = _fact_bundle_product(
        fastmoss_bundle,
        product_id=str(row_context.get("product_id") or row_context.get("product_identity", {}).get("product_id") or ""),
    )
    metrics_snapshot = dict(fastmoss_result.get("metrics_snapshot") or {})
    overview_metrics = dict(metrics_snapshot.get("overview") or {})

    product_id = _first_text(
        tiktok_product.get("product_id"),
        product_result.get("product_id"),
        fastmoss_product.get("product_id"),
        row_context.get("product_id"),
        row_context.get("product_identity", {}).get("product_id") if isinstance(row_context.get("product_identity"), Mapping) else "",
    )
    product_url = _first_text(
        tiktok_product.get("normalized_url"),
        tiktok_product.get("product_url"),
        product_result.get("normalized_product_url"),
        row_context.get("normalized_product_url"),
        row_context.get("product_url"),
        fastmoss_product.get("product_url"),
    )
    title = _first_text(
        logical_fields.get("title"),
        tiktok_product.get("title"),
        fastmoss_product.get("title"),
    )
    seller_name = _first_text(
        logical_fields.get("shop_name"),
        tiktok_product.get("seller_name"),
        tiktok_product.get("shop_name"),
        fastmoss_product.get("seller_name"),
        fastmoss_product.get("shop_name"),
    )
    image_url = _first_text(
        _first_media_asset_url(media_result),
        logical_fields.get("main_image_url"),
        _first_media_asset_url(product_result),
        _first_media_asset_url(fastmoss_bundle),
    )
    price_text = _price_number_text(
        logical_fields.get("price_text"),
        tiktok_product.get("price_text"),
        tiktok_product.get("price_amount"),
        overview_metrics.get("front_price"),
        overview_metrics.get("real_price"),
        overview_metrics.get("price"),
    )
    fastmoss_price = _price_number_text(
        overview_metrics.get("fastmoss_price"),
        overview_metrics.get("real_price"),
        overview_metrics.get("price"),
        price_text,
    )

    fields = {
        "SKU-ID": product_id,
        "产品链接": _normalize_tiktok_product_url(product_url),
        "图片": image_url,
        "标题": title,
        "卖家": seller_name,
        "价格": price_text,
        "Fastmoss价格": fastmoss_price,
        "昨日销量": _first_text(
            _metric_text(
                overview_metrics,
                "yday_sold_count",
                "yesterday_sold_count",
                "day1_sold_count",
                "yday_sales",
                "yesterday_sales",
            ),
            _daily_sales_text(daily_metrics, window_days=1),
        ),
        "近7天销量": _first_text(
            _metric_text(
                overview_metrics,
                "day7_sold_count",
                "sales_7d",
                "day7_sales",
                "sold_count_7d",
            ),
            _daily_sales_text(daily_metrics, window_days=7),
        ),
        "近90天销量": _first_text(
            _metric_text(
                overview_metrics,
                "day90_sold_count",
                "sales_90d",
                "day90_sales",
                "sold_count_90d",
            ),
            _daily_sales_text(daily_metrics, window_days=90),
        ),
    }
    return {key: value for key, value in fields.items() if value not in ("", None, [], {})}

def _competitor_status_text(row_status: str) -> str:
    return {
        "unavailable": "已下架/区域不可售",
    }.get(str(row_status or ""), "")

def _fact_bundle_product(fact_bundle: Mapping[str, Any], *, product_id: str) -> dict[str, Any]:
    products = fact_bundle.get("products") if isinstance(fact_bundle, Mapping) else []
    fallback: dict[str, Any] = {}
    for item in products if isinstance(products, list) else []:
        if not isinstance(item, Mapping):
            continue
        current = dict(item)
        if not fallback:
            fallback = current
        if product_id and str(current.get("product_id") or "") == product_id:
            return current
    return fallback

def _first_media_asset_url(payload: Mapping[str, Any]) -> str:
    assets = []
    if isinstance(payload, Mapping):
        for key in ("media_assets", "synced_assets"):
            value = payload.get(key)
            if isinstance(value, list):
                assets.extend(value)
    for asset in _prefer_main_image_assets(assets):
        if isinstance(asset, Mapping):
            source_url = _first_text(
                asset.get("remote_uri"),
                asset.get("source_url"),
                asset.get("object_key"),
                asset.get("local_path"),
            )
            if source_url:
                return source_url
    for nested_key in ("media_fact_bundle", "fact_bundle"):
        nested = payload.get(nested_key) if isinstance(payload, Mapping) else None
        if isinstance(nested, Mapping):
            found = _first_media_asset_url(nested)
            if found:
                return found
    return ""

def _prefer_main_image_assets(assets: list[Any]) -> list[Any]:
    main_assets: list[Any] = []
    other_assets: list[Any] = []
    for asset in assets if isinstance(assets, list) else []:
        if isinstance(asset, Mapping) and str(asset.get("media_role") or "") == "product_main_image":
            main_assets.append(asset)
        else:
            other_assets.append(asset)
    return [*main_assets, *other_assets]

def _source_fields_from_row_context(row_context: Mapping[str, Any]) -> dict[str, Any]:
    for source in (row_context, row_context.get("source_context")):
        if not isinstance(source, Mapping):
            continue
        fields = source.get("source_fields") or source.get("fields")
        if isinstance(fields, Mapping):
            return dict(fields)
        nested = source.get("source_context")
        if isinstance(nested, Mapping):
            fields = nested.get("source_fields") or nested.get("fields")
            if isinstance(fields, Mapping):
                return dict(fields)
    return {}

def _metric_text(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key) if isinstance(payload, Mapping) else None
        text = _first_text(value)
        if text:
            return text
    return ""

def _daily_sales_text(daily_metrics: list[Mapping[str, Any]], *, window_days: int) -> str:
    if not daily_metrics or window_days <= 0:
        return ""
    ordered = sorted(
        (dict(item) for item in daily_metrics if isinstance(item, Mapping)),
        key=lambda item: _first_text(item.get("metric_date"), item.get("date"), item.get("dt")),
    )
    if len(ordered) < window_days:
        return ""
    selected = ordered[-window_days:]
    values: list[float] = []
    for item in selected:
        value = _number_value(
            item.get("sold_count"),
            dict(item.get("payload") or {}).get("inc_sold_count") if isinstance(item.get("payload"), Mapping) else None,
        )
        if value is None:
            return ""
        values.append(value)
    total = sum(values)
    return str(int(total)) if float(total).is_integer() else str(total)

def _price_number_text(*values: Any) -> str:
    text = ""
    for value in values:
        candidate = _first_text(value)
        if not candidate:
            continue
        if "*" in candidate:
            continue
        text = candidate
        break
    if not text:
        return ""
    normalized = text.strip().replace(",", "")
    normalized = re.sub(r"^(?:US\$|USD\s*|\$|￥|¥|CNY\s*|RMB\s*)", "", normalized, flags=re.IGNORECASE).strip()
    normalized = re.sub(r"\s*(?:USD|US\$|美元|元)$", "", normalized, flags=re.IGNORECASE).strip()
    match = re.search(r"[-+]?\d+(?:\.\d+)?", normalized)
    if match is None:
        return normalized
    number = match.group(0)
    return number.rstrip("0").rstrip(".") if "." in number else number

def _number_value(*values: Any) -> float | None:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        text = _first_text(value).replace(",", "")
        if not text:
            continue
        try:
            return float(text)
        except ValueError:
            continue
    return None

def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
        else:
            text = str(value).strip()
        if text:
            return text
    return ""

def _list_text(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := _first_text(item))]
    if isinstance(value, tuple):
        return [text for item in value if (text := _first_text(item))]
    text = _first_text(value)
    return [text] if text else []

def _has_explicit_identity_lookup(payload: Mapping[str, Any]) -> bool:
    return bool(_first_text(payload.get("product_url"), payload.get("product_id")))

def _has_explicit_record_selection(payload: Mapping[str, Any]) -> bool:
    return bool(_list_text(payload.get("source_record_ids")))

def _normalize_source_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, Mapping):
            continue
        source_record_id = str(
            row.get("source_record_id")
            or row.get("record_id")
            or row.get("recordId")
            or ""
        ).strip()
        product_identity = _resolve_product_identity(row.get("product_identity"), row)
        business_key = str(product_identity.get("business_key") or source_record_id)
        normalized.append(
            {
                "source_record_id": source_record_id or business_key,
                "product_identity": product_identity,
                "product_id": str(product_identity.get("product_id") or ""),
                "product_url": str(product_identity.get("product_url") or ""),
                "normalized_product_url": str(product_identity.get("normalized_product_url") or ""),
                "business_key": business_key,
                "source_context": dict(row),
            }
        )
    return normalized

def _minimal_row_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    identity = _resolve_product_identity(payload.get("product_identity"), payload)
    source_record_id = str(payload.get("source_record_id") or identity.get("business_key") or "")
    return {
        "source_record_id": source_record_id,
        "product_identity": identity,
        "product_id": str(identity.get("product_id") or ""),
        "product_url": str(identity.get("product_url") or ""),
        "normalized_product_url": str(identity.get("normalized_product_url") or ""),
        "business_key": str(identity.get("business_key") or source_record_id),
        "source_context": dict(payload),
    }

def _resolve_product_identity(*sources: Any) -> dict[str, str]:
    product_id = ""
    product_url = ""
    for source in sources:
        product_id = product_id or _lookup_nested(source, "product_id")
        product_url = product_url or _lookup_nested(source, "normalized_product_url", "product_url", "url")
        nested_identity = source.get("product_identity") if isinstance(source, Mapping) else None
        if isinstance(nested_identity, Mapping):
            product_id = product_id or str(nested_identity.get("product_id") or "")
            product_url = product_url or str(
                nested_identity.get("normalized_product_url") or nested_identity.get("product_url") or ""
            )
    if not product_id:
        product_id = _extract_tiktok_product_id(product_url)
    normalized_url = _normalize_tiktok_product_url(product_url)
    if not product_url:
        product_url = normalized_url
    business_key = product_id or normalized_url or product_url
    return {
        "product_id": product_id,
        "product_url": product_url,
        "normalized_product_url": normalized_url or product_url,
        "business_key": business_key,
    }

def _lookup_nested(source: Any, *keys: str) -> str:
    if not isinstance(source, Mapping):
        return ""
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return str(value)
    for nested_key in ("payload", "result", "fields"):
        nested = source.get(nested_key)
        if isinstance(nested, Mapping):
            found = _lookup_nested(nested, *keys)
            if found:
                return found
    return ""

def _effective_tiktok_result(*, tiktok_job: Mapping[str, Any] | None, browser_execution: Any) -> dict[str, Any]:
    if browser_execution is not None and str(browser_execution.status or "") == "success":
        return extract_effective_result_payload(browser_execution)
    return extract_effective_result_payload(tiktok_job)

def _collect_asset_refs(product_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_assets: list[Any] = []
    media_assets = product_result.get("media_assets")
    if isinstance(media_assets, list):
        raw_assets.extend(media_assets)
    images = product_result.get("images")
    if isinstance(images, list):
        raw_assets.extend(images)
    videos = product_result.get("videos")
    if isinstance(videos, list):
        raw_assets.extend(videos)

    assets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for asset in raw_assets:
        if isinstance(asset, Mapping):
            item = dict(asset)
        elif isinstance(asset, str):
            item = {"source_url": asset, "source_type": "image"}
        else:
            continue
        source_url = str(item.get("source_url") or item.get("url") or "").strip()
        local_path = str(item.get("local_path") or "").strip()
        object_key = str(item.get("object_key") or "").strip()
        dedupe_key = source_url or local_path or object_key
        if not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        assets.append(
            _compact_mapping(
                {
                    "source_url": source_url,
                    "source_type": str(item.get("source_type") or item.get("type") or "image"),
                    "file_name": str(item.get("file_name") or ""),
                    "mime_type": str(item.get("mime_type") or ""),
                    "local_path": local_path,
                    "object_key": object_key,
                    "remote_uri": str(item.get("remote_uri") or ""),
                    "entity_type": str(item.get("entity_type") or ""),
                    "entity_external_id": str(item.get("entity_external_id") or item.get("product_id") or ""),
                    "media_role": str(item.get("media_role") or ""),
                    "source_platform": str(item.get("source_platform") or "tiktok"),
                    "metadata": item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {},
                }
            )
        )
    return assets

def _browser_resource_code(candidate: Mapping[str, Any]) -> str:
    business_key = str(candidate.get("business_key") or candidate.get("source_record_id") or "")
    return f"tiktok_product:{business_key}" if business_key else ""

def _fastmoss_browser_resource_code(payload: Mapping[str, Any]) -> str:
    return _first_text(
        payload.get("fastmoss_browser_profile_ref"),
        payload.get("browser_profile_ref"),
        payload.get("profile_ref"),
        "fastmoss:browser",
    )

def _extract_tiktok_product_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"/(?:pdp|product)/(\d+)", text)
    if match:
        return str(match.group(1))
    fallback = re.search(r"(\d{6,})", text)
    return str(fallback.group(1)) if fallback else ""

def _normalize_tiktok_product_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    product_id = _extract_tiktok_product_id(text)
    if not product_id:
        return text
    return f"https://www.tiktok.com/shop/pdp/{product_id}"

def _runtime_child_context(
    *,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "task_code": request.task_code,
        "workflow_code": workflow.workflow_code,
        "stage_code": stage_code,
    }

def _payload_subset(payload: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: payload.get(key)
        for key in keys
        if payload.get(key) not in (None, "", [], {})
    }

def _compact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in values.items() if value not in (None, "", [], {})}

def _fastmoss_settings_from_request_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    settings = dict(payload.get("fastmoss") or {}) if isinstance(payload.get("fastmoss"), Mapping) else {}
    for source_key, target_key in (
        ("fastmoss_phone", "phone"),
        ("fastmoss_password", "password"),
        ("fastmoss_phone_env", "phone_env"),
        ("fastmoss_password_env", "password_env"),
        ("fastmoss_base_url", "base_url"),
        ("region", "region"),
        ("fastmoss_timeout", "timeout"),
        ("fastmoss_window_days", "window_days"),
        ("browser_cookies", "browser_cookies"),
        ("fastmoss_live_fetch", "live_fetch"),
        ("ensure_fastmoss_logged_in", "ensure_logged_in"),
    ):
        if payload.get(source_key) not in (None, "", [], {}):
            settings[target_key] = payload.get(source_key)
    return settings

def _source_table_ref_from_request_payload(payload: Mapping[str, Any]) -> str:
    return _first_text(payload.get("source_table_ref"), payload.get("table_url"))

def _artifact_settings_from_request_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    for source_key, target_key in (
        ("execution_control_artifact_root", "artifact_root"),
        ("execution_control_artifact_bucket", "artifact_bucket"),
        ("execution_control_artifact_store_provider", "artifact_store_provider"),
        ("execution_control_artifact_object_prefix", "artifact_object_prefix"),
        ("execution_control_minio_endpoint", "minio_endpoint"),
        ("execution_control_minio_access_key", "minio_access_key"),
        ("execution_control_minio_secret_key", "minio_secret_key"),
        ("execution_control_minio_region", "minio_region"),
        ("execution_control_minio_secure", "minio_secure"),
        ("execution_control_minio_create_bucket", "minio_create_bucket"),
    ):
        if payload.get(source_key) not in (None, "", [], {}):
            settings[target_key] = payload.get(source_key)
    return settings

def _merge_runtime_fact_bundles(*bundles: Mapping[str, Any]) -> dict[str, Any]:
    merged = {
        **{key: [] for key in FACT_BUNDLE_LIST_KEYS},
        "relations": {key: [] for key in FACT_BUNDLE_RELATION_KEYS},
    }
    for bundle in bundles:
        if not isinstance(bundle, Mapping):
            continue
        for key in FACT_BUNDLE_LIST_KEYS:
            value = bundle.get(key)
            if isinstance(value, list):
                merged[key].extend(dict(item) for item in value if isinstance(item, Mapping))
        relations = bundle.get("relations")
        if isinstance(relations, Mapping):
            for key in FACT_BUNDLE_RELATION_KEYS:
                value = relations.get(key)
                if isinstance(value, list):
                    merged["relations"][key].extend(dict(item) for item in value if isinstance(item, Mapping))
    return merged

def _dedupe_asset_refs(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for asset in assets:
        source_url = str(asset.get("source_url") or "")
        key = source_url or str(asset.get("local_path") or asset.get("object_key") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(asset)
    return deduped

__all__ = [name for name in globals() if not name.startswith('__')]
