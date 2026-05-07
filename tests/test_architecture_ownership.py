from __future__ import annotations

import importlib.util
import ast
from pathlib import Path
import subprocess
import sys
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "dev" / "check_architecture_ownership.py"
PACKAGE_ROOT = REPO_ROOT / "src" / "automation_business_scaffold"
TIKTOK_FLOW_ROOT = PACKAGE_ROOT / "domains" / "tiktok" / "flows"
CAPABILITIES_ROOT = PACKAGE_ROOT / "capabilities"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_architecture_ownership", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "automation_business_scaffold").mkdir(parents=True)
    return root


def _checks_for(root: Path) -> set[str]:
    checker = _load_checker()
    return {finding.check for finding in checker.check_repository(root)}


def _python_sources(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_checker_catches_reexports_thin_wrappers_and_domain_owner_shims(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    package = root / "src" / "automation_business_scaffold"
    _write(package / "owner.py", "def run(value):\n    return value\n")
    _write(
        package / "domains" / "sample" / "mappers" / "legacy_mapper.py",
        "from automation_business_scaffold.owner import run\n\n__all__ = ['run']\n",
    )
    _write(
        package / "domains" / "sample" / "projections" / "thin_projection.py",
        "from automation_business_scaffold.owner import run\n\n"
        "def project(value):\n"
        "    return run(value)\n",
    )

    checks = _checks_for(root)

    assert "domain_owner_files_are_not_reexport_only" in checks
    assert "no_explicit_reexports" in checks
    assert "no_reexport_only_modules" in checks
    assert "no_thin_wrappers" in checks


def test_checker_catches_capability_handler_ownership_violations(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    package = root / "src" / "automation_business_scaffold"
    _write(package / "capabilities" / "_implementations" / "hidden.py", "VALUE = 1\n")
    _write(
        package / "capabilities" / "sample" / "alpha_handlers.py",
        "from automation_business_scaffold.capabilities.sample.beta_handler import helper\n\n"
        "HANDLER_CODE = 'alpha'\n"
        "HANDLER_CODE = 'alpha_duplicate'\n\n"
        "def alpha_handler(context):\n"
        "    return context\n\n"
        "def second_handler(context):\n"
        "    return helper(context)\n",
    )
    _write(
        package / "capabilities" / "sample" / "beta_handler.py",
        "HANDLER_CODE = 'beta'\n\n"
        "def beta_handler(context):\n"
        "    return context\n\n"
        "def helper(context):\n"
        "    return context\n",
    )

    checks = _checks_for(root)

    assert "capability_handlers_do_not_import_handlers" in checks
    assert "no_capabilities_implementations" in checks
    assert "one_handler_code_per_file" in checks
    assert "one_public_handler_per_file" in checks


def test_current_repository_passes_architecture_ownership_checks() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--repo-root", str(REPO_ROOT)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_refactored_tiktok_flow_packages_do_not_import_runtime_repositories_or_provider_clients() -> None:
    guarded_flow_packages = (
        TIKTOK_FLOW_ROOT / "search_keyword_selection_products",
        TIKTOK_FLOW_ROOT / "search_keyword_competitor_products",
    )
    forbidden_import_prefixes = (
        "automation_business_scaffold.infrastructure.runtime.repositories",
        "automation_business_scaffold.infrastructure.feishu",
        "automation_business_scaffold.infrastructure.fastmoss",
        "automation_business_scaffold.infrastructure.tiktok",
        "automation_business_scaffold.capabilities.input_sources.feishu",
        "automation_business_scaffold.capabilities.fact_sources.fastmoss",
        "automation_business_scaffold.capabilities.browser",
    )

    violations: list[str] = []
    for flow_package in guarded_flow_packages:
        for path in _python_sources(flow_package):
            for imported in _imports(path):
                if imported.startswith(forbidden_import_prefixes):
                    violations.append(f"{path.relative_to(REPO_ROOT)} imports {imported}")

    assert violations == []


def test_capabilities_do_not_import_tiktok_business_modules() -> None:
    violations: list[str] = []
    for path in _python_sources(CAPABILITIES_ROOT):
        for imported in _imports(path):
            if imported.startswith("automation_business_scaffold.domains.tiktok"):
                violations.append(f"{path.relative_to(REPO_ROOT)} imports {imported}")

    assert violations == []


def test_refactored_stage_packages_do_not_import_sibling_workflow_packages() -> None:
    guarded_flow_packages = (
        TIKTOK_FLOW_ROOT / "search_keyword_selection_products",
        TIKTOK_FLOW_ROOT / "search_keyword_competitor_products",
    )
    package_prefixes = {
        flow_package: (
            "automation_business_scaffold.domains.tiktok.flows."
            f"{flow_package.name}"
        )
        for flow_package in guarded_flow_packages
    }

    violations: list[str] = []
    for flow_package in guarded_flow_packages:
        sibling_prefixes = [
            prefix
            for package, prefix in package_prefixes.items()
            if package != flow_package
        ]
        for path in _python_sources(flow_package / "stages"):
            for imported in _imports(path):
                if any(imported.startswith(prefix) for prefix in sibling_prefixes):
                    violations.append(f"{path.relative_to(REPO_ROOT)} imports {imported}")

    assert violations == []
