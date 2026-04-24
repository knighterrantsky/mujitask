from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from automation_business_scaffold.contracts.workflow.execution_helpers import (
    extract_effective_result_payload,
    extract_handler_result_status,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


SUCCESS_STATUSES = {"success", "skipped", "partial_success"}
ACTIVE_STATUSES = {"pending", "running", "retry_wait"}


@dataclass(frozen=True, slots=True)
class RuntimeAcceptanceArtifacts:
    runtime_trace: dict[str, Any]
    fact_projection: dict[str, Any]
    feishu_projection: dict[str, Any]
    outbox: dict[str, Any]

    def artifact_values(self, refs: Mapping[str, Any]) -> dict[str, Any]:
        return {
            str(refs["runtime_trace_ref"]): deepcopy(self.runtime_trace),
            str(refs["fact_projection_ref"]): deepcopy(self.fact_projection),
            str(refs["feishu_projection_ref"]): deepcopy(self.feishu_projection),
            str(refs["outbox_ref"]): deepcopy(self.outbox),
        }


def build_runtime_acceptance_artifacts(
    *,
    store: RuntimeStore,
    request_id: str,
    workflow_code: str,
    baseline_trace: Mapping[str, Any],
    feishu_records: Sequence[Mapping[str, Any]],
    baseline_feishu_projection: Mapping[str, Any] | None = None,
) -> RuntimeAcceptanceArtifacts:
    return RuntimeAcceptanceArtifacts(
        runtime_trace=build_runtime_trace_projection(
            store=store,
            request_id=request_id,
            baseline_trace=baseline_trace,
        ),
        fact_projection=build_fact_projection_from_store(store=store, request_id=request_id),
        feishu_projection=build_feishu_projection(
            feishu_records,
            baseline_projection=baseline_feishu_projection,
        ),
        outbox=build_outbox_projection_from_store(
            store=store,
            request_id=request_id,
            workflow_code=workflow_code,
        ),
    )


def build_runtime_trace_projection(
    *,
    store: RuntimeStore,
    request_id: str,
    baseline_trace: Mapping[str, Any],
) -> dict[str, Any]:
    jobs = store.list_api_worker_jobs_for_request(request_id=request_id)
    executions = [execution.to_dict() for execution in store.list_task_executions(request_id=request_id)]
    all_children = [*jobs, *executions]
    baseline_stages = [dict(item) for item in _mapping_list(baseline_trace.get("runtime_stages"))]
    runtime_stages: list[dict[str, Any]] = []
    for stage in baseline_stages:
        stage_code = str(stage.get("stage_code") or "")
        children = [_record for _record in all_children if _stage_code(_record) == stage_code]
        runtime_stages.append(
            {
                "stage_code": stage_code,
                "status": _aggregate_status(children),
                "job_id": _record_id(children[0]) if children else "",
                "updated_at": _max_value(children, "updated_at"),
            }
        )

    unused_indexes = set(range(len(jobs)))
    api_jobs: list[dict[str, Any]] = []
    for expected_job in _mapping_list(baseline_trace.get("api_jobs")):
        expected_code = str(expected_job.get("job_code") or "")
        matched_index = next(
            (
                index
                for index, job in enumerate(jobs)
                if index in unused_indexes and str(job.get("job_code") or "") == expected_code
            ),
            None,
        )
        if matched_index is None:
            api_jobs.append({"job_code": expected_code, "status": "", "job_id": "", "run_id": ""})
            continue
        unused_indexes.remove(matched_index)
        job = jobs[matched_index]
        api_jobs.append(
            {
                "job_code": expected_code,
                "status": _effective_status(job),
                "job_id": str(job.get("job_id") or ""),
                "run_id": str(job.get("run_id") or ""),
            }
        )

    return {"runtime_stages": runtime_stages, "api_jobs": api_jobs}


def build_fact_projection_from_store(*, store: RuntimeStore, request_id: str) -> dict[str, Any]:
    persisted_entities: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    seen_entities: set[tuple[str, str]] = set()
    seen_relations: set[tuple[str, str, str]] = set()
    seen_observations: set[tuple[str, str, str]] = set()

    for job in store.list_api_worker_jobs_for_request(request_id=request_id, job_code="fact_bundle_upsert"):
        if _effective_status(job) not in SUCCESS_STATUSES:
            continue
        result = extract_effective_result_payload(job)
        fact_bundle = _mapping(result.get("fact_bundle"))
        for product in _mapping_list(fact_bundle.get("products")):
            product_id = _first_text(product.get("product_id"), product.get("external_id"))
            _append_entity(persisted_entities, seen_entities, entity_type="product", entity_id=product_id)
        for creator in _mapping_list(fact_bundle.get("creators")):
            creator_id = _first_text(
                creator.get("creator_id"),
                creator.get("unique_id"),
                creator.get("uid"),
                creator.get("creator_key"),
            )
            _append_entity(persisted_entities, seen_entities, entity_type="creator", entity_id=creator_id)
        relation_payload = _mapping(fact_bundle.get("relations"))
        for relation in _mapping_list(relation_payload.get("creator_products")):
            creator_id = _first_text(
                relation.get("creator_id"),
                relation.get("unique_id"),
                relation.get("uid"),
                relation.get("creator_key"),
            )
            product_id = _first_text(relation.get("product_id"), relation.get("product_key"))
            key = ("creator_promotes_product", creator_id, product_id)
            if creator_id and product_id and key not in seen_relations:
                seen_relations.add(key)
                relations.append(
                    {
                        "relation_type": "creator_promotes_product",
                        "from_entity_id": creator_id,
                        "to_entity_id": product_id,
                    }
                )
        for snapshot in _mapping_list(fact_bundle.get("product_metric_snapshots")):
            overview = _mapping(_mapping(snapshot.get("payload")).get("overview"))
            product_id = _first_text(snapshot.get("product_id"))
            for metric_name in ("day7_sold_count", "sales_7d", "sold_count_7d"):
                metric_value = overview.get(metric_name)
                if metric_value not in (None, ""):
                    _append_observation(
                        observations,
                        seen_observations,
                        entity_id=product_id,
                        metric="day7_sold_count",
                        value=metric_value,
                    )
                    break
        for observation in _mapping_list(result.get("observations")):
            entity_id = _strip_entity_prefix(_first_text(observation.get("entity_key"), observation.get("entity_id")))
            metric_name = _first_text(observation.get("metric_name"), observation.get("metric"))
            metric_value = observation.get("metric_value", observation.get("value"))
            if metric_name.endswith("follower_count") or metric_name == "follower_count":
                _append_observation(
                    observations,
                    seen_observations,
                    entity_id=entity_id,
                    metric="follower_count",
                    value=metric_value,
                )

    for job in store.list_api_worker_jobs_for_request(request_id=request_id, job_code="fastmoss_creator_fetch"):
        if _effective_status(job) not in SUCCESS_STATUSES:
            continue
        result = extract_effective_result_payload(job)
        creator_fact = _mapping(result.get("creator_fact_bundle"))
        creator_id = _first_text(
            creator_fact.get("creator_id"),
            creator_fact.get("unique_id"),
            creator_fact.get("uid"),
            _strip_entity_prefix(_first_text(creator_fact.get("entity_key"))),
        )
        _append_entity(persisted_entities, seen_entities, entity_type="creator", entity_id=creator_id)
        for relation in _mapping_list(result.get("product_relations")):
            _append_creator_product_relation(relations, seen_relations, relation)
        for observation in _mapping_list(result.get("observations")):
            _append_creator_observation(observations, seen_observations, observation)

    if any(item.get("metric") == "follower_count" for item in observations):
        observations = [item for item in observations if item.get("metric") == "follower_count"]

    return {
        "persisted_entities": sorted(
            persisted_entities,
            key=lambda item: ({"creator": 0, "product": 1}.get(str(item.get("entity_type") or ""), 9), str(item.get("entity_id") or "")),
        ),
        "relations": relations,
        "observations": observations,
    }


def build_feishu_projection(
    records: Sequence[Mapping[str, Any]],
    *,
    baseline_projection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    baseline = _mapping(baseline_projection)
    baseline_records = _mapping_list(baseline.get("records"))
    normalized_records = [_normalize_feishu_record(record) for record in records]
    if not baseline_records:
        return {
            **({"target_table_ref": _first_text(baseline.get("target_table_ref"))} if baseline.get("target_table_ref") else {}),
            "records": normalized_records,
        }

    ordered: list[dict[str, Any]] = []
    used: set[int] = set()
    for index, baseline_record in enumerate(baseline_records):
        candidate_index = _match_feishu_record_index(
            normalized_records,
            baseline_record=baseline_record,
            used=used,
            fallback_index=index,
        )
        if candidate_index is None:
            continue
        used.add(candidate_index)
        candidate = deepcopy(normalized_records[candidate_index])
        if "target_table_ref" not in baseline_record:
            candidate.pop("target_table_ref", None)
        baseline_fields = _mapping(baseline_record.get("fields"))
        if baseline_fields:
            fields = _mapping(candidate.get("fields"))
            candidate["fields"] = {
                field_name: _canonical_field_value(field_name, fields.get(field_name))
                for field_name in baseline_fields
                if field_name in fields
            }
        ordered.append(candidate)

    projection: dict[str, Any] = {}
    if baseline.get("target_table_ref"):
        projection["target_table_ref"] = baseline.get("target_table_ref")
    projection["records"] = ordered
    return projection


def build_outbox_projection_from_store(
    *,
    store: RuntimeStore,
    request_id: str,
    workflow_code: str,
) -> dict[str, Any]:
    request = store.load_task_request(request_id=request_id)
    outbox_items = store.list_request_outbox(request_id=request_id)
    outbox = outbox_items[0].to_dict() if outbox_items else {}
    return {
        "event_type": str(outbox.get("event_type") or "task_request.completed"),
        "request_id": request_id,
        "summary": _legacy_summary_projection(workflow_code=workflow_code, request=request.to_dict()),
    }


def _legacy_summary_projection(*, workflow_code: str, request: Mapping[str, Any]) -> dict[str, Any]:
    summary = _mapping(request.get("summary"))
    result = _mapping(request.get("result"))
    final_status = _first_text(summary.get("final_status"), request.get("status"))
    if workflow_code == "refresh_current_competitor_table":
        return {
            "final_status": final_status,
            "updated_count": int(result.get("row_success_count") or 0),
            "failed_count": int(result.get("row_failed_count") or 0),
        }
    if workflow_code == "search_keyword_competitor_products":
        return {
            "final_status": final_status,
            "candidate_total_count": int(result.get("candidate_total_count") or summary.get("candidate_total_count") or 0),
            "seeded_count": int(result.get("seed_total_count") or 0),
            "skipped_duplicate_count": 0,
        }
    if workflow_code == "sync_tk_influencer_pool":
        product_groups = _mapping_list(summary.get("product_groups"))
        return {
            "final_status": final_status,
            "processed_product_count": int(summary.get("product_group_count") or len(product_groups)),
            "upserted_creator_count": sum(int(group.get("influencer_write_success_count") or 0) for group in product_groups),
            "partial_failure_count": sum(1 for group in product_groups if str(group.get("final_status") or "") not in {"", "success"}),
        }
    return {"final_status": final_status}


def _normalize_feishu_record(record: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        _canonical_field_name(key): _canonical_field_value(_canonical_field_name(key), value)
        for key, value in _mapping(record.get("fields")).items()
    }
    item = {
        "record_id": _first_text(record.get("record_id")),
        "fields": fields,
    }
    target_table_ref = _first_text(record.get("target_table_ref"))
    if target_table_ref:
        item["target_table_ref"] = target_table_ref
    return item


def _match_feishu_record_index(
    records: Sequence[Mapping[str, Any]],
    *,
    baseline_record: Mapping[str, Any],
    used: set[int],
    fallback_index: int,
) -> int | None:
    baseline_record_id = _first_text(baseline_record.get("record_id"))
    baseline_target = _first_text(baseline_record.get("target_table_ref"))
    for index, record in enumerate(records):
        if index in used:
            continue
        if baseline_record_id and _first_text(record.get("record_id")) == baseline_record_id:
            return index
        if baseline_target and _first_text(record.get("target_table_ref")) == baseline_target:
            return index
    if fallback_index < len(records) and fallback_index not in used:
        return fallback_index
    return None


def _canonical_field_name(name: Any) -> str:
    return {
        "产品链接": "商品链接",
        "标题": "商品名称",
        "记录时间": "记录日期",
    }.get(str(name or ""), str(name or ""))


def _canonical_field_value(field_name: str, value: Any) -> Any:
    if isinstance(value, Mapping):
        if "link" in value or "text" in value:
            return _first_text(value.get("link"), value.get("text"))
        return {str(key): _canonical_field_value(str(key), child) for key, child in value.items()}
    if isinstance(value, list):
        return [_canonical_field_value(field_name, item) for item in value]
    if field_name == "达人查找状态" and value == "已完成":
        return "已找到"
    if field_name in {"粉丝数", "关联商品销量"}:
        normalized_number = _normalize_display_number(value)
        if normalized_number is not None:
            return str(normalized_number)
    return value


def _aggregate_status(records: Sequence[Mapping[str, Any]]) -> str:
    if not records:
        return ""
    statuses = [_effective_status(record) for record in records]
    if any(status in {"failed", "cancelled"} for status in statuses):
        return "failed"
    if any(status in ACTIVE_STATUSES for status in statuses):
        return "running"
    if all(status in SUCCESS_STATUSES for status in statuses):
        return "success"
    return statuses[-1] if statuses else ""


def _effective_status(record: Mapping[str, Any]) -> str:
    handler_status = extract_handler_result_status(record)
    return handler_status or str(record.get("status") or "")


def _stage_code(record: Mapping[str, Any]) -> str:
    payload = _mapping(record.get("payload"))
    return _first_text(payload.get("stage_code"), record.get("stage"))


def _record_id(record: Mapping[str, Any]) -> str:
    return _first_text(record.get("job_id"), record.get("execution_id"))


def _max_value(records: Sequence[Mapping[str, Any]], key: str) -> Any:
    values = [record.get(key) for record in records if record.get(key) not in (None, "")]
    return max(values) if values else ""


def _append_entity(
    entities: list[dict[str, Any]],
    seen: set[tuple[str, str]],
    *,
    entity_type: str,
    entity_id: str,
) -> None:
    if not entity_id:
        return
    key = (entity_type, entity_id)
    if key in seen:
        return
    seen.add(key)
    entities.append({"entity_type": entity_type, "entity_id": entity_id})


def _append_creator_product_relation(
    relations: list[dict[str, Any]],
    seen: set[tuple[str, str, str]],
    relation: Mapping[str, Any],
) -> None:
    creator_id = _strip_entity_prefix(
        _first_text(relation.get("from_entity_id"), relation.get("from_entity_key"), relation.get("creator_id"))
    )
    product_id = _strip_entity_prefix(
        _first_text(relation.get("to_entity_id"), relation.get("to_entity_key"), relation.get("product_id"))
    )
    key = ("creator_promotes_product", creator_id, product_id)
    if not creator_id or not product_id or key in seen:
        return
    seen.add(key)
    relations.append(
        {
            "relation_type": "creator_promotes_product",
            "from_entity_id": creator_id,
            "to_entity_id": product_id,
        }
    )


def _append_creator_observation(
    observations: list[dict[str, Any]],
    seen: set[tuple[str, str, str]],
    observation: Mapping[str, Any],
) -> None:
    metric_name = _first_text(observation.get("metric_name"), observation.get("metric"))
    if not (metric_name.endswith("follower_count") or metric_name == "follower_count"):
        return
    _append_observation(
        observations,
        seen,
        entity_id=_strip_entity_prefix(_first_text(observation.get("entity_key"), observation.get("entity_id"))),
        metric="follower_count",
        value=observation.get("metric_value", observation.get("value")),
    )


def _append_observation(
    observations: list[dict[str, Any]],
    seen: set[tuple[str, str, str]],
    *,
    entity_id: str,
    metric: str,
    value: Any,
) -> None:
    if not entity_id or not metric or value in (None, ""):
        return
    key = (entity_id, metric, str(value))
    if key in seen:
        return
    seen.add(key)
    observations.append({"entity_id": entity_id, "metric": metric, "value": value})


def _mapping(value: Any) -> dict[str, Any]:
    return {str(key): deepcopy(child) for key, child in value.items()} if isinstance(value, Mapping) else {}


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _strip_entity_prefix(value: str) -> str:
    text = _first_text(value)
    prefixes = ("creator:", "product:", "fastmoss_creator:", "fastmoss_product:", "tiktok_product:")
    while True:
        for prefix in prefixes:
            if text.startswith(prefix):
                text = text.removeprefix(prefix)
                break
        else:
            return text


def _normalize_display_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)
    text = str(value or "").strip().replace(",", "").replace(" ", "")
    multiplier = 1.0
    lower = text.lower()
    for suffix, factor in (("w", 10_000.0), ("万", 10_000.0), ("k", 1_000.0), ("m", 1_000_000.0), ("亿", 100_000_000.0)):
        if lower.endswith(suffix):
            multiplier = factor
            text = text[: -len(suffix)]
            break
    try:
        number = float(text) * multiplier
    except ValueError:
        return None
    return int(number) if number.is_integer() else number
