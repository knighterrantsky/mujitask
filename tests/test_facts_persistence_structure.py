from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from automation_business_scaffold.infrastructure.facts.fact_payload_views import extract_fact_payloads
from automation_business_scaffold.infrastructure.facts.tk_fact_ingestion_service import TKFactIngestionService


REPO_ROOT = Path(__file__).resolve().parents[1]
FACTS_ROOT = REPO_ROOT / "src" / "automation_business_scaffold" / "infrastructure" / "facts"


class _FakeFactStore:
    def __init__(self) -> None:
        self.products: dict[str, dict[str, Any]] = {}
        self.raw_rows: list[dict[str, Any]] = []
        self.raw_links: list[dict[str, Any]] = []

    def record_raw_api_response(self, **kwargs: Any) -> dict[str, Any]:
        row = {"raw_response_id": f"raw-{len(self.raw_rows) + 1}", **kwargs}
        self.raw_rows.append(row)
        return row

    def upsert_product(self, **kwargs: Any) -> dict[str, Any]:
        product_id = str(kwargs["product_id"])
        row = self.products.setdefault(product_id, {"id": f"product-{product_id}", "product_id": product_id})
        row.update({key: value for key, value in kwargs.items() if value not in ("", None, {}, [])})
        return dict(row)

    def link_raw_entity(self, **kwargs: Any) -> dict[str, Any]:
        row = {"raw_link_id": f"raw-link-{len(self.raw_links) + 1}", **kwargs}
        self.raw_links.append(row)
        return row


def test_fact_ingestion_success_path_writes_product_and_raw_link() -> None:
    fact_store = _FakeFactStore()

    result = TKFactIngestionService(fact_store=fact_store).ingest_api_response(
        source_platform="fastmoss",
        source_endpoint="goods.detail",
        request_params={"product_id": "p-1"},
        response_payload={"ok": True},
        products=[{"product_id": "p-1", "title": "Demo product"}],
    )

    assert result["fact_entities"][0]["id"] == "product-p-1"
    assert result["fact_entities"][0]["product_id"] == "p-1"
    assert result["fact_entities"][0]["title"] == "Demo product"
    assert result["fact_entities"][0]["source_platform"] == "fastmoss"
    assert result["raw_api_responses"][0]["raw_response_id"] == "raw-1"
    assert result["raw_api_responses"][1]["entity_external_id"] == "p-1"


def test_fact_ingestion_duplicate_product_path_is_idempotent() -> None:
    fact_store = _FakeFactStore()
    service = TKFactIngestionService(fact_store=fact_store)

    first = service.ingest_api_response(
        source_platform="fastmoss",
        source_endpoint="goods.detail",
        products=[{"product_id": "p-1", "title": "First title"}],
    )
    second = service.ingest_api_response(
        source_platform="fastmoss",
        source_endpoint="goods.detail",
        products=[{"product_id": "p-1", "title": "Updated title"}],
    )

    assert first["fact_entities"][0]["id"] == second["fact_entities"][0]["id"]
    assert second["fact_entities"][0]["title"] == "Updated title"
    assert list(fact_store.products) == ["p-1"]


def test_fact_payload_view_dedupes_aggregate_read_model() -> None:
    product = {"id": "product-p-1", "product_id": "p-1"}
    relation = {"relation_key": "p-1:shop-1"}
    raw = {"raw_response_id": "raw-1"}

    result = extract_fact_payloads(
        [
            {"fact_entities": [product], "fact_relations": [relation], "raw_api_responses": [raw]},
            {"fact_entities": [dict(product)], "fact_relations": [dict(relation)], "raw_api_responses": [dict(raw)]},
        ]
    )

    assert result["fact_entities"] == [product]
    assert result["fact_relations"] == [relation]
    assert result["raw_api_responses"] == [raw]


def test_facts_persistence_modules_have_layered_ownership() -> None:
    store_source = (FACTS_ROOT / "tk_fact_store.py").read_text(encoding="utf-8")
    ingestion_source = (FACTS_ROOT / "tk_fact_ingestion_service.py").read_text(encoding="utf-8")
    query_source = (FACTS_ROOT / "fact_queries.py").read_text(encoding="utf-8")
    service_source = (FACTS_ROOT / "tk_fact_service.py").read_text(encoding="utf-8")

    store_tree = ast.parse(store_source)
    ingestion_tree = ast.parse(ingestion_source)
    service_tree = ast.parse(service_source)
    store_methods = {
        child.name
        for node in ast.walk(store_tree)
        if isinstance(node, ast.ClassDef) and node.name == "TKFactStore"
        for child in node.body
        if isinstance(child, ast.FunctionDef)
    }
    ingestion_module_functions = {node.name for node in ingestion_tree.body if isinstance(node, ast.FunctionDef)}
    service_functions = {node.name for node in service_tree.body if isinstance(node, ast.FunctionDef)}

    assert {"find_media_asset", "creator_has_product", "get_product", "get_creator", "get_raw_api_response"}.isdisjoint(store_methods)
    assert ingestion_module_functions == set()
    assert service_functions == set()
    assert "upsert_product" not in query_source
    assert "INSERT INTO" not in query_source
    assert (FACTS_ROOT / "fact_queries.py").is_file()
    assert (FACTS_ROOT / "fact_payload_views.py").is_file()
    assert (FACTS_ROOT / "ingestion_payloads.py").is_file()
    assert (FACTS_ROOT / "fact_bundle_ingestion.py").is_file()
