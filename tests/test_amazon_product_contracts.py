from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_yaml(relative_path: str) -> dict[str, Any]:
    path = REPO_ROOT / relative_path
    assert path.is_file(), f"missing contract: {relative_path}"
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{relative_path} must be a YAML mapping"
    return loaded


def _read(relative_path: str) -> str:
    path = REPO_ROOT / relative_path
    assert path.is_file(), f"missing document: {relative_path}"
    return path.read_text(encoding="utf-8")


def test_formal_requirement_and_domain_route_are_declared() -> None:
    requirement = _read(
        "docs/business/requirements/amazon-product-detail-collection.md"
    )
    domain_route = _read("docs/domains/amazon-product-detail/README.md")

    required_requirement_tokens = (
        "状态: 已批准，实施中",
        "refresh_amazon_product_row_by_asin",
        "美国站",
        "^[A-Z0-9]{10}$",
        "同一飞书 record_id",
        "批量和搜索不在首期实现范围",
    )
    assert all(token in requirement for token in required_requirement_tokens)

    required_route_refs = (
        "../../business/requirements/amazon-product-detail-collection.md",
        "../../arch/workflow-amazon-product-detail-design.md",
        "../../../contracts/fields/feishu-amazon-products.yaml",
        "../../../contracts/states/amazon-product-collection-status.yaml",
        "../../../contracts/workflow/refresh_amazon_product_row_by_asin.yaml",
    )
    assert all(reference in domain_route for reference in required_route_refs)


def test_workflow_contract_freezes_codes_stages_and_handlers() -> None:
    contract = _load_yaml(
        "contracts/workflow/refresh_amazon_product_row_by_asin.yaml"
    )

    assert contract["task_code"] == "refresh_amazon_product_row_by_asin"
    assert contract["workflow_code"] == "refresh_amazon_product_row_by_asin"
    assert [stage["stage_code"] for stage in contract["stages"]] == [
        "read_amazon_product_row",
        "collect_amazon_product_detail",
        "persist_amazon_product_detail",
        "ready_for_summary",
    ]
    assert set(contract["new_handlers"]) == {
        "amazon_product_browser_fetch",
        "amazon_product_row_persist",
        "amazon_product_fact_upsert",
    }
    assert contract["identity"] == {
        "marketplace_code": "US",
        "domain": "amazon.com",
        "asin_normalization": "trim_uppercase",
        "asin_pattern": "^[A-Z0-9]{10}$",
        "canonical_url_template": "https://www.amazon.com/dp/{asin}",
    }
    assert contract["persistence"] == {
        "requires_fact_db": True,
        "requires_object_storage": True,
        "runtime_result_policy": "compact_references_only",
    }
    assert contract["payload"]["business_inputs_only"] is True
    assert contract["payload"]["required"] == ["table_ref", "source_record_id"]
    assert contract["payload"]["allowed"] == ["table_ref", "source_record_id"]
    assert contract["payload"]["additional_properties"] is False


def test_feishu_field_and_state_ownership_is_explicit() -> None:
    fields_contract = _load_yaml("contracts/fields/feishu-amazon-products.yaml")
    states_contract = _load_yaml(
        "contracts/states/amazon-product-collection-status.yaml"
    )

    assert fields_contract["table_alias"] == "AMAZON_PRODUCTS"
    assert fields_contract["identity"] == {
        "marketplace_code": "US",
        "field": "ASIN",
        "normalization": "trim_uppercase",
        "pattern": "^[A-Z0-9]{10}$",
        "projection_may_write": False,
    }
    fields = {field["name"]: field for field in fields_contract["fields"]}
    assert fields["ASIN"]["owner"] == "business_input"
    assert fields["强制刷新"]["owner"] == "business_input"
    assert fields["来源关键词"]["owner"] == "amazon_search_workflow"
    assert fields["采集状态"]["owner"] == "amazon_workflow"
    projection_fields = {
        "商品链接",
        "标题",
        "品牌",
        "主图",
        "图库",
        "当前价格",
        "库存状态",
        "Parent ASIN",
        "Child ASIN列表",
        "BSR排名",
        "技术参数",
        "页面ASIN",
        "字段完整度",
    }
    assert all(fields[name]["owner"] == "amazon_projection" for name in projection_fields)
    assert fields_contract["writeback_rules"]["missing"] == "preserve_existing"
    assert fields_contract["writeback_rules"]["record_target"] == (
        "source_record_id"
    )

    states = {state["code"]: state for state in states_contract["states"]}
    assert set(states) == {
        "pending",
        "collecting",
        "persisting",
        "success",
        "partial_success",
        "unavailable",
        "blocked",
        "failed",
    }
    assert {code for code, state in states.items() if state["terminal"]} == {
        "success",
        "partial_success",
        "unavailable",
        "blocked",
        "failed",
    }
    assert states_contract["runtime_boundary"]["blocked_is_runtime_status"] is False
    assert states_contract["transitions"] == {
        "pending": ["collecting", "failed"],
        "collecting": ["persisting", "blocked", "failed"],
        "persisting": ["success", "partial_success", "unavailable", "failed"],
    }


def test_amazon_fact_tables_and_object_prefixes_are_isolated() -> None:
    contract = _load_yaml("contracts/facts/product-fact-collection.yaml")
    amazon = contract["platform_contracts"]["amazon_us"]

    assert amazon["identity_key"] == ["marketplace_code", "asin"]
    assert amazon["marketplace_code"] == "US"
    assert set(amazon["fact_tables"]) == {
        "amazon_products",
        "amazon_product_snapshots",
        "amazon_offer_snapshots",
        "amazon_product_variants",
        "amazon_bsr_snapshots",
        "amazon_media_assets",
        "amazon_product_media_assets",
        "amazon_raw_captures",
        "amazon_feishu_bindings",
    }
    assert amazon["object_prefixes"] == {
        "runtime_evidence": "<env>/runs/<run_id>/amazon/<asin>/",
        "raw_capture": "<env>/raw-captures/amazon/us/<asin>/",
        "product_media": "<env>/product-media/amazon/us/<asin>/",
    }
    assert amazon["runtime_db_schema_change"] is False
    assert amazon["cross_platform_foreign_keys"] is False


def test_amazon_owner_boundaries_are_registered() -> None:
    contract = _load_yaml("contracts/harness/architecture-ownership.yaml")
    owners = contract["owners"]

    helper_paths = {
        entry["path"] for entry in owners["business_flow"]["allowed_helper_like_paths"]
    }
    assert {
        "src/automation_business_scaffold/domains/amazon/flows/refresh_amazon_product_row_by_asin/orchestrator.py",
        "src/automation_business_scaffold/domains/amazon/flows/amazon_product_row_persist/orchestrator.py",
    } <= helper_paths

    assert owners["amazon_browser_capture"]["paths"] == [
        "src/automation_business_scaffold/capabilities/browser/amazon/**",
        "src/automation_business_scaffold/capabilities/browser/amazon_product_fetch_handler.py",
    ]
    assert "write Fact DB" in owners["amazon_browser_capture"]["forbidden"]
    assert owners["amazon_fact_persistence"]["paths"] == [
        "src/automation_business_scaffold/infrastructure/schemas/amazon_fact_schema.py",
        "src/automation_business_scaffold/infrastructure/facts/amazon_fact_store.py",
        "src/automation_business_scaffold/capabilities/persistence/database/amazon_product_fact_upsert_handler.py",
    ]
    assert "decide Feishu fields" in owners["amazon_fact_persistence"]["forbidden"]


def test_both_amazon_roadmap_features_are_in_progress_and_gated() -> None:
    roadmap = _load_yaml("contracts/harness/code-roadmap.yaml")
    features = {feature["feature_code"]: feature for feature in roadmap["features"]}

    schema = features["amazon_product_fact_schema"]
    ingest = features["amazon_single_product_ingest"]
    for feature in (schema, ingest):
        assert feature["status"] == "in_progress"
        assert feature["requires_architecture_delta_gate"] is True
        assert feature["source_contracts"]
        assert feature["allowed_paths"]
        assert feature["done_gate"]["tests"]
        assert feature["done_gate"]["commands"]

    assert {
        "alembic/versions/20260714_0007_amazon_product_facts.py",
        "src/automation_business_scaffold/infrastructure/schemas/amazon_fact_schema.py",
        "src/automation_business_scaffold/infrastructure/schemas/__init__.py",
        "src/automation_business_scaffold/infrastructure/facts/amazon_fact_store.py",
        "src/automation_business_scaffold/capabilities/persistence/database/amazon_product_fact_upsert_handler.py",
        "tests/conftest.py",
    } <= set(schema["allowed_paths"])
    fact_owned_paths = {
        "alembic/versions/20260714_0007_amazon_product_facts.py",
        "src/automation_business_scaffold/infrastructure/schemas/amazon_fact_schema.py",
        "src/automation_business_scaffold/infrastructure/facts/amazon_fact_store.py",
        "src/automation_business_scaffold/capabilities/persistence/database/amazon_product_fact_upsert_handler.py",
    }
    assert fact_owned_paths.isdisjoint(ingest["allowed_paths"])
    assert "contracts/facts/product-fact-collection.yaml" in ingest["source_contracts"]
    assert {
        "src/automation_business_scaffold/capabilities/browser/amazon/product_page.py",
        "src/automation_business_scaffold/capabilities/browser/amazon_product_fetch_handler.py",
        "src/automation_business_scaffold/domains/amazon/tasks/refresh_amazon_product_row_by_asin.py",
        "src/automation_business_scaffold/domains/amazon/workflows/refresh_amazon_product_row_by_asin.py",
        "tests/test_runtime_amazon_product_business_e2e.py",
    } <= set(ingest["allowed_paths"])

    schema_tests = [
        "tests/test_amazon_product_contracts.py",
        "tests/test_amazon_fact_schema.py",
        "tests/test_amazon_fact_store.py",
        "tests/test_amazon_product_fact_upsert_handler.py",
    ]
    assert schema["done_gate"]["tests"] == schema_tests
    assert {
        token
        for command in schema["done_gate"]["commands"]
        for token in command.split()
        if token.startswith("tests/")
    } == set(schema_tests)

    ingest_tests = [
        "tests/test_amazon_product_contracts.py",
        "tests/test_amazon_product_page.py",
        "tests/test_amazon_product_browser_fetch_handler.py",
        "tests/test_feishu_amazon_product_mapping.py",
        "tests/test_amazon_product_row_persist.py",
        "tests/test_refresh_amazon_product_row_by_asin.py",
        "tests/test_runtime_amazon_product_ingest.py",
        "tests/test_runtime_amazon_product_business_e2e.py",
        "tests/test_handler_registry_contract.py",
        "tests/test_workflow_architecture_manifests.py",
        "tests/test_harness_code_roadmap.py",
        "tests/test_architecture_ownership.py",
    ]
    assert ingest["done_gate"]["tests"] == ingest_tests
    assert {
        token
        for command in ingest["done_gate"]["commands"]
        for token in command.split()
        if token.startswith("tests/")
    } == set(ingest_tests)


def test_contract_index_and_design_status_match_implementation_phase() -> None:
    contract_index = _read("contracts/README.md")
    design = _read("docs/arch/workflow-amazon-product-detail-design.md")

    assert "feishu-amazon-products.yaml" in contract_index
    assert "amazon-product-collection-status.yaml" in contract_index
    assert "refresh_amazon_product_row_by_asin.yaml" in contract_index
    assert "状态: 已批准，实施中，能力尚未完成" in design
    assert "代码、migration 和机器契约完成并通过 completion gate 前" in design
