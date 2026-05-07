from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "automation_business_scaffold"
TIKTOK_FLOW_ROOT = PACKAGE_ROOT / "domains" / "tiktok" / "flows"

REFACTORED_FLOWS = (
    "search_keyword_selection_products",
    "search_keyword_competitor_products",
    "refresh_current_competitor_table",
    "sync_tk_influencer_pool",
    "tiktok_fastmoss_product_ingest",
)
REQUIRED_CONTEXT_MODULES = (
    "models.py",
    "runtime_views.py",
    "stage_inputs.py",
    "decision_models.py",
    "summary_inputs.py",
)
FORBIDDEN_CONTEXT_MODULE_NAMES = {
    "utils.py",
    "helper.py",
    "helpers.py",
    "common.py",
    "shared.py",
    "_implementations.py",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _python_sources(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(_read(path), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _is_empty_or_docstring_only(path: Path) -> bool:
    source = _read(path).strip()
    if not source:
        return True
    tree = ast.parse(source, filename=str(path))
    return (
        len(tree.body) == 1
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    )


def test_phase_6_flows_use_context_packages_not_giant_context_files() -> None:
    for flow_name in REFACTORED_FLOWS:
        flow_package = TIKTOK_FLOW_ROOT / flow_name
        context_package = flow_package / "context"

        assert flow_package.is_dir(), flow_name
        assert not (flow_package / "context.py").exists(), flow_name
        assert context_package.is_dir(), flow_name
        assert (context_package / "__init__.py").is_file(), flow_name
        assert _is_empty_or_docstring_only(context_package / "__init__.py"), flow_name
        for module_name in REQUIRED_CONTEXT_MODULES:
            assert (context_package / module_name).is_file(), f"{flow_name}/{module_name}"


def test_context_packages_have_no_dumping_ground_modules() -> None:
    found: list[str] = []
    for flow_name in REFACTORED_FLOWS:
        for path in _python_sources(TIKTOK_FLOW_ROOT / flow_name / "context"):
            if path.name in FORBIDDEN_CONTEXT_MODULE_NAMES:
                found.append(path.relative_to(REPO_ROOT).as_posix())

    assert found == []


def test_context_submodules_do_not_own_runtime_writes_dispatch_or_sending() -> None:
    forbidden_tokens = (
        "enqueue_api_worker_jobs",
        "enqueue_task_executions",
        "update_task_request",
        "update_request_stage_cursor",
        "_update_request_cursor",
        "create_notification_outbox",
        "build_tiktok_outbox_message_text",
    )
    violations: list[str] = []
    for flow_name in REFACTORED_FLOWS:
        for path in _python_sources(TIKTOK_FLOW_ROOT / flow_name / "context"):
            if path.name == "__init__.py":
                continue
            source = _read(path)
            for token in forbidden_tokens:
                if token in source:
                    violations.append(f"{path.relative_to(REPO_ROOT)} contains {token}")

    assert violations == []


def test_context_submodules_do_not_import_forbidden_layers() -> None:
    forbidden_prefixes = (
        "automation_business_scaffold.infrastructure",
        "automation_business_scaffold.capabilities",
        "automation_business_scaffold.control_plane",
    )
    violations: list[str] = []
    for flow_name in REFACTORED_FLOWS:
        for path in _python_sources(TIKTOK_FLOW_ROOT / flow_name / "context"):
            for imported in _imports(path):
                if imported.startswith(forbidden_prefixes):
                    violations.append(f"{path.relative_to(REPO_ROOT)} imports {imported}")

    assert violations == []


def test_stage_summary_and_orchestrator_import_concrete_context_submodules() -> None:
    violations: list[str] = []
    for flow_name in REFACTORED_FLOWS:
        flow_package = TIKTOK_FLOW_ROOT / flow_name
        for path in _python_sources(flow_package):
            if "context" in path.parts:
                continue
            source = _read(path)
            forbidden_imports = (
                "from .context import",
                "from ..context import",
                "import .context",
                "import ..context",
            )
            for token in forbidden_imports:
                if token in source:
                    violations.append(f"{path.relative_to(REPO_ROOT)} uses {token}")

    assert violations == []
