# Platform-managed: this test keeps the vendored contract pack aligned with manifest metadata.

import json
import tomllib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_platform_manifest_matches_contract_pack_version():
    manifest = yaml.safe_load((ROOT / ".platform" / "platform-manifest.yaml").read_text(encoding="utf-8"))
    version = manifest["public_contract_pack_version"]
    contract_dir = ROOT / "docs" / "framework_contract" / version

    assert contract_dir.exists()
    assert "framework_repo_url" not in manifest
    assert "framework_commit" not in manifest
    assert "framework_version" not in manifest
    assert (contract_dir / "public-import-surface.md").exists()
    assert (contract_dir / "public-capability-status.md").exists()
    assert (contract_dir / "public-timeline.md").exists()
    assert (contract_dir / "public-migration-guide.md").exists()
    assert (contract_dir / "business-consumption-contract.md").exists()
    assert (contract_dir / "workflow-runtime-contract.md").exists()
    assert (contract_dir / "workflow-draft-contract.md").exists()


def test_pyproject_pins_framework_dependency_and_example_files_exist():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    framework_dependencies = [
        dependency for dependency in dependencies if dependency.startswith("automation-framework @ ")
    ]

    assert len(framework_dependencies) == 1
    assert framework_dependencies[0] == (
        "automation-framework @ git+https://github.com/knighterrantsky/automation-framework.git@v0.3.6"
    )

    profiles = json.loads((ROOT / "config" / "browser_profiles.example.json").read_text(encoding="utf-8"))
    assert "local-chrome" in profiles
    assert (ROOT / "examples" / "workflow_draft.review-only.yaml").exists()
