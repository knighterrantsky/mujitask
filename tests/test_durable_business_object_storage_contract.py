from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
STORAGE_CONTRACT = (
    REPO_ROOT / "contracts" / "facts" / "durable-business-object-storage.yaml"
)


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_minio_is_default_deny_for_long_term_business_objects_only() -> None:
    contract = _load_yaml(STORAGE_CONTRACT)

    assert contract["status"] == "active_contract"
    assert contract["contract_revision"] == 2
    assert contract["implementation_status"] == "complete"
    assert contract["implementation_gaps"] == []
    assert contract["scope"] == {
        "platforms": ["tiktok", "amazon"],
        "production_provider": "minio",
        "bucket_policy": "reuse_configured_bucket_without_runtime_bucket_creation",
        "policy_mode": "default_deny_explicit_allowlist",
    }
    assert contract["classification"]["runtime_transient_file"]["minio_write"] == "forbidden"
    assert contract["runtime_artifact_policy"]["storage"] == "local_only"
    assert contract["runtime_artifact_policy"]["minio_sync"] == "forbidden"


def test_durable_reference_never_depends_on_local_temp_paths() -> None:
    contract = _load_yaml(STORAGE_CONTRACT)
    durable_ref = contract["durable_reference"]

    assert durable_ref["authoritative_fields"] == [
        "bucket",
        "object_key",
        "content_digest",
    ]
    assert durable_ref["content_digest_format"] == "64_lowercase_hex"
    assert durable_ref["bucket_validation"] == "exact_configured_bucket"
    assert (
        durable_ref["object_key_validation"]
        == "exact_allowlisted_class_prefix_or_template"
    )
    assert "local_path" in durable_ref["forbidden_authoritative_locators"]
    assert durable_ref["read_rule"] == (
        "downstream_consumers_read_minio_by_complete_durable_reference"
    )
    assert durable_ref["local_first_or_fallback_read"] == "forbidden"
    assert durable_ref["infer_missing_bucket_from_global_config"] == "forbidden"


def test_cutover_rejects_legacy_repair_backfill_and_mixed_workers() -> None:
    contract = _load_yaml(STORAGE_CONTRACT)
    cutover = contract["cutover_policy"]

    assert cutover["mode"] == "hard_cutover_without_legacy_data_compatibility"
    assert cutover["incomplete_reference_rule"] == (
        "invalid_cache_miss_and_rematerialize_from_source"
    )
    assert cutover["bucket_inference"] == "forbidden"
    assert cutover["legacy_repair_or_backfill"] == "forbidden"
    assert cutover["legacy_data_migration"] == "forbidden"
    assert cutover["mixed_version_workers"] == "forbidden"
    assert cutover["deployment_sequence"] == [
        "stop_all_old_workers",
        "deploy_contract_revision_2",
        "start_single_version_workers",
    ]
    assert "compatibility" not in contract


def test_tiktok_and_amazon_share_the_same_allowlist_boundary() -> None:
    contract = _load_yaml(STORAGE_CONTRACT)
    allowed = contract["allowed_object_classes"]
    denied = set(contract["denied_object_classes"])

    assert set(allowed) == {
        "tiktok_product_media",
        "tiktok_creator_avatar",
        "tiktok_video_cover",
        "tiktok_business_attachment",
        "amazon_product_media",
        "amazon_normalized_capture",
        "amazon_blocked_evidence_screenshot",
    }
    assert not set(allowed) & denied
    assert all("retention_policy" in spec for spec in allowed.values())
    assert allowed["tiktok_business_attachment"]["business_binding"] == (
        "explicit_contract_defined_fact_or_business_record_attachment"
    )
    assert allowed["tiktok_business_attachment"]["admission"] == (
        "explicit_fact_or_business_record_attachment_contract_required"
    )
    assert {
        "stdout_or_stderr_log",
        "state_dump",
        "success_page_html",
        "page_data",
        "network_data",
        "generic_raw_api_response",
        "feishu_raw_table_snapshot",
        "temporary_download",
    }.issubset(denied)


def test_amazon_success_keeps_only_normalized_capture_and_business_media() -> None:
    contract = _load_yaml(STORAGE_CONTRACT)
    amazon = contract["amazon_policy"]

    assert amazon["success_like_outcomes"] == ["success", "partial_success"]
    assert amazon["success_required_objects"] == ["amazon_normalized_capture"]
    assert amazon["success_allowed_objects"] == [
        "amazon_normalized_capture",
        "amazon_product_media",
    ]
    assert amazon["partial_success_allowed_objects"] == [
        "amazon_normalized_capture",
        "amazon_product_media",
    ]
    assert amazon["blocked_required_objects"] == [
        "amazon_blocked_evidence_screenshot"
    ]
    assert amazon["blocked_allowed_objects"] == [
        "amazon_blocked_evidence_screenshot"
    ]
    assert set(amazon["success_forbidden_objects"]) == {
        "success_page_html",
        "page_data",
        "network_data",
        "ordinary_or_success_screenshot",
    }

    workflow = _load_yaml(
        REPO_ROOT
        / "contracts"
        / "workflow"
        / "refresh_amazon_product_row_by_asin.yaml"
    )["persistence"]["object_storage_policy"]
    aliases = amazon["workflow_object_aliases"]
    assert [aliases[name] for name in workflow["success_allowed"]] == (
        amazon["success_allowed_objects"]
    )
    assert [aliases[name] for name in workflow["blocked_allowed"]] == (
        amazon["blocked_allowed_objects"]
    )


def test_existing_owners_write_allowlisted_objects_without_new_storage_helper() -> None:
    contract = _load_yaml(STORAGE_CONTRACT)
    ownership = contract["writer_ownership"]
    architecture = _load_yaml(
        REPO_ROOT / "contracts" / "harness" / "architecture-ownership.yaml"
    )["owners"]

    assert set(ownership) >= {
        "media_asset_sync",
        "amazon_browser_capture",
        "forbidden_writers",
        "new_parallel_storage_helper",
    }
    assert ownership["new_parallel_storage_helper"] == "forbidden"
    assert "runtime_supervisor" in ownership["forbidden_writers"]
    assert architecture["object_storage_transport"]["allowed_logical_writers"] == [
        "media_asset_sync",
        "amazon_browser_capture",
    ]
    owned_classes = {
        object_class
        for owner in ("media_asset_sync", "amazon_browser_capture")
        for object_class in ownership[owner]["allowed_classes"]
    }
    assert owned_classes == set(contract["allowed_object_classes"])
    assert (
        "classify a file as a long-term business object"
        in architecture["object_storage_transport"]["forbidden"]
    )


def test_storage_docs_and_platform_contract_reference_the_policy() -> None:
    storage_doc = (
        REPO_ROOT / "docs" / "arch" / "storage-architecture-design.md"
    ).read_text(encoding="utf-8")
    product_contract = _load_yaml(
        REPO_ROOT / "contracts" / "facts" / "product-fact-collection.yaml"
    )

    assert "默认拒绝" in storage_doc
    assert "`bucket + object_key + content_digest`" in storage_doc
    assert "local_path" in storage_doc
    assert product_contract["durable_object_storage_contract"] == (
        "contracts/facts/durable-business-object-storage.yaml"
    )


def test_design_contract_has_a_bounded_cross_platform_completion_gate() -> None:
    roadmap = _load_yaml(
        REPO_ROOT / "contracts" / "harness" / "code-roadmap.yaml"
    )
    feature = {
        item["feature_code"]: item for item in roadmap["features"]
    }["durable_business_object_storage_design_contract"]

    assert feature["status"] == "complete"
    assert feature["feature_type"] == "repository_governance"
    assert "src/**" in feature["forbidden_paths"]
    assert {
        "docs/arch/system-architecture-design.md",
        "docs/arch/project-architecture-contract.md",
        "docs/arch/module-ownership-contract.md",
        "docs/arch/database-architecture-design.md",
        "docs/arch/handler-contract-design.md",
        "docs/arch/runtime-db-schema-design.md",
        "docs/arch/runtime-control-plane-contract.md",
        "docs/arch/entry-output-contract-design.md",
        "docs/arch/workflow-design-guidelines.md",
        "docs/arch/interactive-staged-workflow-runtime-design.md",
        "docs/arch/workflow-selection-table-design.md",
        "docs/arch/workflow-competitor-table-design.md",
        "docs/arch/workflow-influencer-pool-sync-design.md",
        "docs/arch/workflow-amazon-product-detail-design.md",
        "docs/dev/project-configuration.md",
        "docs/dev/dependencies.md",
        "docs/dev/local-development.md",
        "docs/domains/amazon-product-detail/README.md",
        "docs/reference/third-party-services.md",
        "docs/ops/deployment.md",
        "docs/ops/runtime-db-connection-stability.md",
        "contracts/facts/durable-business-object-storage.yaml",
        "contracts/workflow/refresh_current_competitor_table.yaml",
        "contracts/workflow/refresh_competitor_row_by_url.yaml",
        "contracts/workflow/search_keyword_competitor_products.yaml",
        "contracts/workflow/search_keyword_selection_products.yaml",
        "contracts/workflow/tiktok_fastmoss_product_ingest.yaml",
    }.issubset(feature["source_contracts"])
    assert "tests/test_durable_business_object_storage_contract.py" in (
        feature["done_gate"]["tests"]
    )


def test_hard_cutover_implementation_has_a_bounded_completion_gate() -> None:
    roadmap = _load_yaml(
        REPO_ROOT / "contracts" / "harness" / "code-roadmap.yaml"
    )
    feature = {
        item["feature_code"]: item for item in roadmap["features"]
    }["durable_business_object_storage_hard_cutover"]

    assert feature["status"] == "complete"
    assert feature["feature_type"] == "architecture_implementation"
    assert feature["requires_architecture_delta_gate"] is True
    assert {
        "alembic/versions/20260723_0008_tk_durable_media_references.py",
        "src/automation_business_scaffold/capabilities/media/asset_sync_handler.py",
        "src/automation_business_scaffold/capabilities/input_sources/feishu/field_envelopes.py",
        "src/automation_business_scaffold/capabilities/browser/amazon_product_fetch_handler.py",
        "src/automation_business_scaffold/infrastructure/artifacts/artifact_sync.py",
        "tests/test_artifact_sync.py",
    }.issubset(feature["allowed_paths"])
    assert {
        "tests/test_durable_business_object_storage_contract.py",
        "tests/test_media_asset_sync_handler.py",
        "tests/test_amazon_product_browser_fetch_handler.py",
        "tests/test_tk_fact_store.py",
        "tests/test_architecture_delta_gate.py",
    }.issubset(feature["done_gate"]["tests"])
    assert "src/automation_business_scaffold/agent.py" in feature["forbidden_paths"]
    assert "src/automation_business_scaffold/registry.py" in feature["forbidden_paths"]


def test_cross_platform_design_docs_do_not_restore_generic_minio_artifacts() -> None:
    paths = [
        "docs/arch/system-architecture-design.md",
        "docs/arch/database-architecture-design.md",
        "docs/arch/handler-contract-design.md",
        "docs/arch/runtime-db-schema-design.md",
        "docs/arch/runtime-control-plane-contract.md",
        "docs/arch/entry-output-contract-design.md",
        "docs/arch/workflow-design-guidelines.md",
        "docs/arch/interactive-staged-workflow-runtime-design.md",
        "docs/arch/workflow-selection-table-design.md",
        "docs/arch/workflow-competitor-table-design.md",
        "docs/arch/workflow-influencer-pool-sync-design.md",
        "docs/arch/workflow-amazon-product-detail-design.md",
    ]
    combined = "\n".join(
        (REPO_ROOT / path).read_text(encoding="utf-8") for path in paths
    )

    assert '"store_raw_rows": true' not in combined
    assert "s3://runtime-artifacts" not in combined
    assert "full capture and HTML remain in object storage" not in combined
    assert "Browser 成功至少产生 content-addressed normalized JSON 与 sanitized HTML" not in combined
    assert "图片、截图、HTML、raw JSON、artifact 转存" not in combined
    assert "完整飞书行必须进入 artifact/object storage" not in combined
    assert "normalized_product_result and artifact refs" not in combined
    assert "对象内容 | MinIO 或本地文件系统" not in combined
    assert "artifact://" not in combined
    assert "normalized result、artifact evidence" not in combined
    assert "大文件内容不放数据库，放 MinIO" not in combined
    assert "所有大对象通过 artifact ref" not in combined
    assert "mujitask-runtime" not in combined
    assert "店铺图、视频封面等；只要被采集到" not in combined
    assert "tiktok-screenshot" not in combined


def test_tiktok_workflow_contracts_do_not_depend_on_browser_artifacts() -> None:
    paths = [
        "contracts/workflow/refresh_current_competitor_table.yaml",
        "contracts/workflow/refresh_competitor_row_by_url.yaml",
        "contracts/workflow/search_keyword_competitor_products.yaml",
        "contracts/workflow/search_keyword_selection_products.yaml",
        "contracts/workflow/tiktok_fastmoss_product_ingest.yaml",
    ]
    combined = "\n".join(
        (REPO_ROOT / path).read_text(encoding="utf-8") for path in paths
    )

    assert "normalized_product_result and artifact refs" not in combined
    assert "local diagnostic artifact indexes are not downstream inputs" in combined


def test_configuration_and_operations_docs_apply_the_storage_boundary() -> None:
    config_doc = (REPO_ROOT / "docs/dev/project-configuration.md").read_text(
        encoding="utf-8"
    )
    dependencies_doc = (REPO_ROOT / "docs/dev/dependencies.md").read_text(
        encoding="utf-8"
    )
    local_doc = (REPO_ROOT / "docs/dev/local-development.md").read_text(
        encoding="utf-8"
    )
    services_doc = (
        REPO_ROOT / "docs/reference/third-party-services.md"
    ).read_text(encoding="utf-8")
    amazon_domain_doc = (
        REPO_ROOT / "docs/domains/amazon-product-detail/README.md"
    ).read_text(encoding="utf-8")
    deployment_doc = (REPO_ROOT / "docs/ops/deployment.md").read_text(
        encoding="utf-8"
    )
    runtime_stability_doc = (
        REPO_ROOT / "docs/ops/runtime-db-connection-stability.md"
    ).read_text(encoding="utf-8")

    assert "生产 `MINIO_CREATE_BUCKET` 必须为 `false`" in config_doc
    assert "`SYNC_REFERENCED_FILES` 只控制当前运行是否下载或物化被引用的文件" in config_doc
    assert "不是旧数据兼容开关" in config_doc
    assert "正式 workflow 缺 Runtime DB、Fact DB 或对象存储配置时" not in config_doc
    assert "Artifact object store" not in dependencies_doc
    assert "运行时产物" not in dependencies_doc
    assert "完整 Runtime 流程" not in local_doc
    assert "不承载通用 Runtime artifact" in services_doc
    assert "durable-business-object-storage.yaml" in amazon_domain_doc
    assert "持久对象 contract revision 2" in deployment_doc
    assert "禁止混合版本滚动发布" in deployment_doc
    assert "生产 worker 不创建通用 smoke object" in deployment_doc
    assert "仅产生本地诊断文件的 workflow 不要求 MinIO" in runtime_stability_doc
