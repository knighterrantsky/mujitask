# Platform-managed: this test keeps framework dependency metadata aligned.

import json
import tomllib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_platform_manifest_points_to_framework_docs_source():
    manifest = yaml.safe_load((ROOT / ".platform" / "platform-manifest.yaml").read_text(encoding="utf-8"))

    assert manifest["framework_docs_source"] == "automation-framework package or framework repository"
    assert "public_contract_pack_version" not in manifest
    assert "contract_docs" not in manifest
    assert "framework_repo_url" not in manifest
    assert "framework_commit" not in manifest
    assert "framework_version" not in manifest

    protected_paths = set(manifest.get("protected_paths", []))
    assert "docs/framework_contract/**" not in protected_paths
    assert not (ROOT / "docs" / "framework_contract").exists()


def test_pyproject_pins_framework_dependency_and_example_files_exist():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    framework_dependencies = [
        dependency
        for dependency in dependencies
        if dependency.startswith(("automation-framework @ ", "automation-framework["))
    ]

    assert len(framework_dependencies) == 1
    assert framework_dependencies[0] == (
        "automation-framework[captcha] @ git+https://github.com/knighterrantsky/automation-framework.git@v0.3.8"
    )

    profiles = json.loads((ROOT / "config" / "browser_profiles.example.json").read_text(encoding="utf-8"))
    assert "local-chrome" in profiles
    assert (ROOT / "examples" / "workflow_draft.review-only.yaml").exists()
