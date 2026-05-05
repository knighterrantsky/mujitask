from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "harness" / "validate_architecture_delta.py"
CLAIM_DONE = REPO_ROOT / "scripts" / "harness" / "claim_done.py"
ARCHITECTURE_OWNERSHIP = REPO_ROOT / "contracts" / "harness" / "architecture-ownership.yaml"
PRODUCT_FACT_COLLECTION = REPO_ROOT / "contracts" / "facts" / "product-fact-collection.yaml"


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("validate_architecture_delta", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)


def _minimal_architecture_contract() -> str:
    return yaml.safe_dump(
        {
            "schema_version": 1,
            "roadmap_status": "domains_runtime_rewrite",
            "owners": {},
            "legacy_reference_only": [],
        },
        allow_unicode=True,
        sort_keys=False,
    )


def _minimal_product_contract() -> str:
    return yaml.safe_dump(
        {
            "schema_version": 1,
            "contract_type": "product_fact_collection",
            "status": "active_contract",
            "forbidden_new_modules_by_default": [
                "src/automation_business_scaffold/domains/**/facts/*collection*.py",
                "src/automation_business_scaffold/domains/**/*helper*.py",
                "src/automation_business_scaffold/domains/**/*service*.py",
                "src/automation_business_scaffold/domains/**/*manager*.py",
                "src/automation_business_scaffold/domains/**/*coordinator*.py",
            ],
        },
        allow_unicode=True,
        sort_keys=False,
    )


def _repo(tmp_path: Path, *, include_product_contract: bool = True) -> Path:
    root = tmp_path / "repo"
    _init_repo(root)
    _write(root / "contracts" / "harness" / "architecture-ownership.yaml", _minimal_architecture_contract())
    if include_product_contract:
        _write(root / "contracts" / "facts" / "product-fact-collection.yaml", _minimal_product_contract())
    return root


def _run_gate(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--repo-root", str(root)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stdout)


def _failed_checks(payload: dict[str, Any]) -> set[str]:
    return {str(item["check"]) for item in payload["failed_checks"]}


def test_architecture_ownership_yaml_parses() -> None:
    loaded = yaml.safe_load(ARCHITECTURE_OWNERSHIP.read_text(encoding="utf-8"))

    assert loaded["schema_version"] == 1
    assert loaded["roadmap_status"] == "stable"
    assert "business_flow" in loaded["owners"]


def test_product_fact_collection_yaml_parses() -> None:
    loaded = yaml.safe_load(PRODUCT_FACT_COLLECTION.read_text(encoding="utf-8"))

    assert loaded["schema_version"] == 1
    assert loaded["contract_type"] == "product_fact_collection"
    assert "media_rules" in loaded


def test_helper_like_file_name_detection() -> None:
    gate = _load_gate()

    assert gate._is_helper_like_path("src/automation_business_scaffold/domains/tiktok/facts/product_fact_collection.py")
    assert gate._is_helper_like_path("src/automation_business_scaffold/domains/tiktok/foo_service.py")
    assert not gate._is_helper_like_path("src/automation_business_scaffold/domains/tiktok/jobs/keyword_seed_import.py")


def test_product_fact_collection_helper_fails_without_allow_contract(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(
        root / "src" / "automation_business_scaffold" / "domains" / "tiktok" / "facts" / "product_fact_collection.py",
        "def collect_product_facts():\n    return {}\n",
    )

    result = _run_gate(root)
    payload = _payload(result)

    assert result.returncode != 0
    assert payload["claim"] == "not_complete"
    assert "helper_like_added_files_require_contract_allow" in _failed_checks(payload)
    assert "forbidden_new_module_patterns" in _failed_checks(payload)


def test_product_media_code_delta_requires_product_fact_contract(tmp_path: Path) -> None:
    root = _repo(tmp_path, include_product_contract=False)
    _write(
        root / "src" / "automation_business_scaffold" / "capabilities" / "media" / "asset_sync_handler.py",
        "def sync():\n    return {'media_asset': 'x', 'fact_bundle': {}, 'object_key': 'media/x'}\n",
    )

    result = _run_gate(root)
    payload = _payload(result)

    assert result.returncode != 0
    assert payload["claim"] == "not_complete"
    assert "product_fact_collection_contract_parses" in _failed_checks(payload)
    assert "product_fact_collection_contract_required_for_media_delta" in _failed_checks(payload)


def test_architecture_delta_gate_outputs_passed_and_failed_checks(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    result = _run_gate(root)
    payload = _payload(result)

    assert "passed_checks" in payload
    assert "failed_checks" in payload
    assert isinstance(payload["passed_checks"], list)
    assert isinstance(payload["failed_checks"], list)


def test_claim_done_treats_architecture_delta_failure_as_not_complete(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(
        root / "src" / "automation_business_scaffold" / "domains" / "tiktok" / "facts" / "product_fact_collection.py",
        "def collect_product_facts():\n    return {}\n",
    )
    roadmap = {
        "schema_version": 1,
        "roadmap_status": "domains_runtime_rewrite",
        "features": [
            {
                "feature_code": "failing_architecture_delta",
                "status": "complete",
                "change_type": "implementation",
                "default_context": ["AGENTS.md"],
                "source_contracts": ["AGENTS.md"],
                "allowed_paths": ["src/automation_business_scaffold/domains/**"],
                "forbidden_paths": [],
                "done_gate": {
                    "tests": ["tests/test_completion_claim_gate.py"],
                    "commands": [f"{sys.executable} -c \"print('done gate command ran')\""],
                },
            }
        ],
    }
    roadmap_path = tmp_path / "code-roadmap.yaml"
    roadmap_path.write_text(yaml.safe_dump(roadmap, allow_unicode=True), encoding="utf-8")
    env = os.environ.copy()
    env["HARNESS_ARCHITECTURE_DELTA_REPO_ROOT"] = str(root)
    result = subprocess.run(
        [
            sys.executable,
            str(CLAIM_DONE),
            "failing_architecture_delta",
            "--roadmap",
            str(roadmap_path),
            "--run-gates",
        ],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    payload = _payload(result)

    assert result.returncode != 0
    assert payload["claim"] == "not_complete"
    assert "architecture_delta_gate" in _failed_checks(payload)
