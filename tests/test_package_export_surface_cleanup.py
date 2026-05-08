from __future__ import annotations

import ast
import importlib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "automation_business_scaffold"
TIKTOK_FLOW_ROOT = PACKAGE_ROOT / "domains" / "tiktok" / "flows"

TOUCHED_FLOW_PACKAGES = (
    "refresh_current_competitor_table",
    "sync_tk_influencer_pool",
    "tiktok_fastmoss_product_ingest",
    "search_keyword_selection_products",
    "search_keyword_competitor_products",
)
PACKAGE_FLOW_NAMES = (
    *TOUCHED_FLOW_PACKAGES,
    "selection_row_refresh",
    "competitor_row_refresh",
)
BUSINESS_EXPORT_NAMES = {
    "advance_stage",
    "finalize_request",
    "release_request_after_child_completion",
    "advance_sync_tk_influencer_pool_request",
    "dispatch_sync_tk_influencer_pool_request",
    "release_sync_tk_influencer_pool_request",
    "finalize_sync_tk_influencer_pool_request",
    "TASK_CODE",
    "WORKFLOW_CODE",
    "READ_STAGE_CODE",
    "SUMMARY_STAGE_CODE",
}


def _python_sources(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _is_empty_or_docstring_only(path: Path) -> bool:
    source = path.read_text(encoding="utf-8").strip()
    if not source:
        return True
    tree = ast.parse(source, filename=str(path))
    return (
        len(tree.body) == 1
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    )


def test_touched_flow_package_init_files_are_not_business_export_surfaces() -> None:
    for package_name in PACKAGE_FLOW_NAMES:
        init_file = TIKTOK_FLOW_ROOT / package_name / "__init__.py"
        assert init_file.is_file(), package_name
        assert _is_empty_or_docstring_only(init_file), package_name

        source = init_file.read_text(encoding="utf-8")
        assert "__all__" not in source, package_name
        assert " import *" not in source, package_name
        assert ".orchestrator import" not in source, package_name
        assert ".summary import" not in source, package_name
        assert ".context." not in source, package_name


def test_touched_flow_packages_do_not_expose_business_symbols() -> None:
    for package_name in PACKAGE_FLOW_NAMES:
        module = importlib.import_module(
            f"automation_business_scaffold.domains.tiktok.flows.{package_name}"
        )
        exposed = [name for name in BUSINESS_EXPORT_NAMES if hasattr(module, name)]

        assert exposed == [], package_name


def test_tiktok_flows_root_init_is_not_a_business_export_surface() -> None:
    init_file = TIKTOK_FLOW_ROOT / "__init__.py"

    assert _is_empty_or_docstring_only(init_file)

    source = init_file.read_text(encoding="utf-8")
    assert "__all__" not in source
    assert "control_plane.executor.runner" not in source


def test_static_governance_references_use_package_flow_paths() -> None:
    old_path_tokens = {
        f"`flows/{flow_name}.py`"
        for flow_name in PACKAGE_FLOW_NAMES
    } | {
        f"domains/tiktok/flows/{flow_name}.py"
        for flow_name in PACKAGE_FLOW_NAMES
    } | {
        f"src/automation_business_scaffold/domains/tiktok/flows/{flow_name}.py"
        for flow_name in PACKAGE_FLOW_NAMES
    } | {
        f"/flows/{flow_name}.py"
        for flow_name in PACKAGE_FLOW_NAMES
    }
    allowed_antiregression_tests = {
        "tests/test_selection_row_refresh_package_structure.py",
        "tests/test_competitor_row_refresh_package_structure.py",
    }
    scanned_roots = (
        REPO_ROOT / "contracts",
        REPO_ROOT / "docs",
        REPO_ROOT / "src" / "automation_business_scaffold" / "contracts",
        REPO_ROOT / "tests",
    )
    suffixes = {".md", ".yaml", ".yml", ".json", ".py"}
    violations: list[str] = []

    for root in scanned_roots:
        for path in sorted(item for item in root.rglob("*") if item.suffix in suffixes):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in allowed_antiregression_tests:
                continue
            source = path.read_text(encoding="utf-8")
            for token in old_path_tokens:
                if token in source:
                    violations.append(f"{rel} contains old flow path {token}")

    assert violations == []


def test_runtime_imports_target_concrete_modules_for_touched_flows() -> None:
    package_prefix = "automation_business_scaffold.domains.tiktok.flows"
    touched_modules = {
        f"{package_prefix}.{package_name}"
        for package_name in TOUCHED_FLOW_PACKAGES
    }
    violations: list[str] = []

    for path in _python_sources(REPO_ROOT / "src") + _python_sources(REPO_ROOT / "tests"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in touched_modules:
                imported_names = ", ".join(alias.name for alias in node.names)
                violations.append(
                    f"{path.relative_to(REPO_ROOT)} imports {imported_names} from {node.module}"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in touched_modules:
                        violations.append(
                            f"{path.relative_to(REPO_ROOT)} imports package {alias.name}"
                        )

    assert violations == []


def test_runtime_import_strings_target_concrete_modules_for_touched_flows() -> None:
    package_prefix = "automation_business_scaffold.domains.tiktok.flows"
    touched_modules = {
        f"{package_prefix}.{package_name}"
        for package_name in TOUCHED_FLOW_PACKAGES
    }
    quoted_touched_modules = {
        f'"{module_name}"' for module_name in touched_modules
    } | {
        f"'{module_name}'" for module_name in touched_modules
    }
    violations: list[str] = []

    for path in _python_sources(REPO_ROOT / "src") + _python_sources(REPO_ROOT / "tests"):
        source = path.read_text(encoding="utf-8")
        for token in quoted_touched_modules:
            if token in source:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains runtime string {token}")

    assert violations == []
