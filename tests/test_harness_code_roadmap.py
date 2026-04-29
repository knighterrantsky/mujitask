from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
ROADMAP = REPO_ROOT / "contracts" / "harness" / "code-roadmap.yaml"


def _load_roadmap() -> dict[str, Any]:
    loaded = yaml.safe_load(ROADMAP.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), "code-roadmap.yaml must be a YAML mapping"
    return loaded


def _path_exists(value: str) -> bool:
    path = value.rstrip("/")
    if path.endswith("/**"):
        path = path.removesuffix("/**")
    return (REPO_ROOT / path).exists()


def test_code_roadmap_parses() -> None:
    roadmap = _load_roadmap()
    assert roadmap["schema_version"] == 1
    assert roadmap["current_phase"] == "domains_runtime_rewrite"


def test_feature_codes_are_unique() -> None:
    roadmap = _load_roadmap()
    codes = [feature["feature_code"] for feature in roadmap["features"]]
    assert len(codes) == len(set(codes))


def test_default_context_paths_exist() -> None:
    roadmap = _load_roadmap()
    missing = [
        path
        for feature in roadmap["features"]
        for path in feature.get("default_context", [])
        if not _path_exists(path)
    ]
    assert missing == [], "default_context references missing paths:\n" + "\n".join(missing)


def test_source_contract_paths_exist() -> None:
    roadmap = _load_roadmap()
    missing = [
        path
        for feature in roadmap["features"]
        for path in feature.get("source_contracts", [])
        if not _path_exists(path)
    ]
    assert missing == [], "source_contracts references missing paths:\n" + "\n".join(missing)


def test_every_feature_has_done_gate() -> None:
    roadmap = _load_roadmap()
    missing = [
        feature["feature_code"]
        for feature in roadmap["features"]
        if not feature.get("done_gate")
    ]
    assert missing == [], "features are missing done_gate:\n" + "\n".join(missing)


def test_business_is_not_canonical_runtime_owner() -> None:
    roadmap = _load_roadmap()
    owners = roadmap["canonical_runtime_owners"]
    assert all("src/automation_business_scaffold/business" not in owner for owner in owners)


def test_domains_are_canonical_runtime_owner() -> None:
    roadmap = _load_roadmap()
    owners = roadmap["canonical_runtime_owners"]
    assert "src/automation_business_scaffold/domains/**" in owners


def test_business_is_legacy_reference_only() -> None:
    roadmap = _load_roadmap()
    legacy_paths = roadmap["legacy_reference_only"]
    assert "docs/business/**" in legacy_paths
