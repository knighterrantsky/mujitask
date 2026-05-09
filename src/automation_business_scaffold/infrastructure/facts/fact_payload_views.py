from __future__ import annotations

from typing import Any, Mapping, Sequence

"""Aggregate persisted fact payload views for contracts/facts/product-fact-collection.yaml."""


def extract_fact_payloads(items: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    fact_entities: list[dict[str, Any]] = []
    fact_relations: list[dict[str, Any]] = []
    fact_media_assets: list[dict[str, Any]] = []
    fact_metric_observations: list[dict[str, Any]] = []
    raw_api_responses: list[dict[str, Any]] = []
    seen: dict[str, set[str]] = {
        "entities": set(),
        "relations": set(),
        "media": set(),
        "metrics": set(),
        "raw": set(),
    }

    for item in items:
        for entity in item.get("fact_entities", []) if isinstance(item.get("fact_entities"), list) else []:
            key = _fact_identity(entity)
            if key and key not in seen["entities"]:
                seen["entities"].add(key)
                fact_entities.append(dict(entity))
        for relation in item.get("fact_relations", []) if isinstance(item.get("fact_relations"), list) else []:
            key = _fact_identity(relation)
            if key and key not in seen["relations"]:
                seen["relations"].add(key)
                fact_relations.append(dict(relation))
        for asset in item.get("fact_media_assets", []) if isinstance(item.get("fact_media_assets"), list) else []:
            key = _fact_identity(asset)
            if key and key not in seen["media"]:
                seen["media"].add(key)
                fact_media_assets.append(dict(asset))
        for metric in (
            item.get("fact_metric_observations", [])
            if isinstance(item.get("fact_metric_observations"), list)
            else []
        ):
            key = _fact_identity(metric)
            if key and key not in seen["metrics"]:
                seen["metrics"].add(key)
                fact_metric_observations.append(dict(metric))
        for raw_response in item.get("raw_api_responses", []) if isinstance(item.get("raw_api_responses"), list) else []:
            key = _fact_identity(raw_response)
            if key and key not in seen["raw"]:
                seen["raw"].add(key)
                raw_api_responses.append(dict(raw_response))

    return {
        "fact_entities": fact_entities,
        "fact_relations": fact_relations,
        "fact_media_assets": fact_media_assets,
        "fact_metric_observations": fact_metric_observations,
        "raw_api_responses": raw_api_responses,
    }


def _fact_identity(payload: Mapping[str, Any]) -> str:
    for key in (
        "id",
        "product_id",
        "shop_key",
        "creator_key",
        "video_key",
        "asset_id",
        "latest_id",
        "observation_id",
        "relation_key",
        "raw_response_id",
        "raw_link_id",
    ):
        value = _clean_text(payload.get(key))
        if value:
            return f"{key}:{value}"
    return ""


def _clean_text(value: Any) -> str:
    return str(value or "").strip()
