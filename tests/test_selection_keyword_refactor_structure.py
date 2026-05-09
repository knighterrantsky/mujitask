from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "automation_business_scaffold"
SELECTION_FLOW_PACKAGE = PACKAGE_ROOT / "domains" / "tiktok" / "flows" / "search_keyword_selection_products"
SELECTION_TASK = PACKAGE_ROOT / "domains" / "tiktok" / "tasks" / "search_keyword_selection_products.py"
SELECTION_WORKFLOW = PACKAGE_ROOT / "domains" / "tiktok" / "workflows" / "search_keyword_selection_products.py"
SELECTION_ORCHESTRATOR = SELECTION_FLOW_PACKAGE / "orchestrator.py"
SELECTION_CONTEXT = SELECTION_FLOW_PACKAGE / "context"
SELECTION_SUMMARY = SELECTION_FLOW_PACKAGE / "summary.py"
RUNTIME_STORE = PACKAGE_ROOT / "infrastructure" / "runtime" / "runtime_store.py"
RUNTIME_BOOTSTRAP = PACKAGE_ROOT / "infrastructure" / "runtime" / "bootstrap.py"
CAPABILITIES_ROOT = PACKAGE_ROOT / "capabilities"

STAGE_CODES = (
    "keyword_seed_import",
    "fastmoss_security_browser_fallback",
    "dispatch_selection_row_refresh_jobs",
    "refresh_selection_rows",
    "selection_row_browser_fallback",
    "resume_selection_rows_after_browser_fallback",
    "ready_for_summary",
)


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
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_selection_keyword_flow_is_stage_oriented_package() -> None:
    assert SELECTION_FLOW_PACKAGE.is_dir()
    assert not SELECTION_FLOW_PACKAGE.with_suffix(".py").exists()
    assert SELECTION_SUMMARY.is_file()
    for stage_code in STAGE_CODES:
        stage_module = SELECTION_FLOW_PACKAGE / "stages" / f"{stage_code}.py"
        assert stage_module.is_file()
        source = _read(stage_module)
        assert f'STAGE_CODE = "{stage_code}"' in source
        assert "def advance(" in source


def test_selection_keyword_stage_modules_own_stage_logic() -> None:
    orchestrator_source = _read(SELECTION_ORCHESTRATOR)
    assert "def _advance_" not in orchestrator_source
    assert len(orchestrator_source.splitlines()) <= 220

    for stage_code in STAGE_CODES:
        stage_module = SELECTION_FLOW_PACKAGE / "stages" / f"{stage_code}.py"
        source = _read(stage_module)
        assert "import orchestrator" not in source
        assert "orchestrator._advance" not in source

    summary_source = _read(SELECTION_SUMMARY)
    assert "def finalize_request(" in summary_source
    assert "build_outbox_message_text" in summary_source


def test_selection_keyword_new_package_files_remain_scoped() -> None:
    max_lines = {
        "orchestrator.py": 220,
        "context/models.py": 180,
        "context/runtime_views.py": 260,
        "context/stage_inputs.py": 300,
        "context/decision_models.py": 100,
        "context/summary_inputs.py": 60,
        "summary.py": 340,
        "stages/keyword_seed_import.py": 220,
        "stages/fastmoss_security_browser_fallback.py": 180,
        "stages/dispatch_selection_row_refresh_jobs.py": 80,
        "stages/refresh_selection_rows.py": 120,
        "stages/selection_row_browser_fallback.py": 420,
        "stages/resume_selection_rows_after_browser_fallback.py": 140,
        "stages/ready_for_summary.py": 40,
    }
    for relative_path, limit in max_lines.items():
        path = SELECTION_FLOW_PACKAGE / relative_path
        assert len(_read(path).splitlines()) <= limit, relative_path


def test_selection_keyword_task_and_workflow_shells_remain_thin() -> None:
    task_source = _read(SELECTION_TASK)
    workflow_source = _read(SELECTION_WORKFLOW)

    assert len(task_source.splitlines()) <= 80
    assert "run_search_keyword_selection_products_request" in task_source
    assert "StageDefinition(" in workflow_source
    assert "FeishuBitableClient" not in workflow_source
    assert "FastMossClient" not in workflow_source
    assert "create_engine" not in workflow_source


def test_selection_keyword_flow_package_does_not_import_transport_or_runtime_persistence() -> None:
    forbidden_import_prefixes = (
        "automation_business_scaffold.infrastructure.runtime",
        "automation_business_scaffold.infrastructure.feishu",
        "automation_business_scaffold.infrastructure.fastmoss",
        "automation_business_scaffold.infrastructure.tiktok",
        "automation_business_scaffold.capabilities.input_sources.feishu",
        "automation_business_scaffold.capabilities.fact_sources.fastmoss",
        "automation_business_scaffold.capabilities.browser",
    )
    violations: list[str] = []
    for path in _python_sources(SELECTION_FLOW_PACKAGE):
        for imported in _imports(path):
            if imported.startswith(forbidden_import_prefixes):
                violations.append(f"{path.relative_to(REPO_ROOT)} imports {imported}")
    assert violations == []


def test_capabilities_do_not_import_tiktok_business_modules_directly() -> None:
    violations: list[str] = []
    for path in _python_sources(CAPABILITIES_ROOT):
        for imported in _imports(path):
            if imported.startswith("automation_business_scaffold.domains.tiktok"):
                violations.append(f"{path.relative_to(REPO_ROOT)} imports {imported}")
    assert violations == []


def test_selection_keyword_package_has_no_generic_dumping_ground_modules() -> None:
    forbidden_names = {"utils.py", "helper.py", "helpers.py", "common.py", "manager.py", "service.py"}
    found = [path.relative_to(REPO_ROOT).as_posix() for path in _python_sources(SELECTION_FLOW_PACKAGE) if path.name in forbidden_names]
    assert found == []


def test_runtime_store_facade_delegates_first_persistence_boundaries() -> None:
    runtime_source = _read(RUNTIME_STORE)
    assert "RequestStatusQuery(self)" in runtime_source
    assert "NotificationOutboxRepository(self)" in runtime_source
    assert "ResourceLeaseRepository(self)" in runtime_source
    assert "WatchdogQuery(self)" in runtime_source
    assert "bootstrap_runtime_schema(self._engine)" in runtime_source
    assert "ensure_runtime_schema(self._engine)" not in runtime_source


def test_runtime_bootstrap_is_explicit_schema_owner() -> None:
    bootstrap_source = _read(RUNTIME_BOOTSTRAP)
    runtime_source = _read(RUNTIME_STORE)
    init_node = next(
        node
        for node in ast.walk(ast.parse(runtime_source))
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    init_calls = {
        child.func.id if isinstance(child.func, ast.Name) else child.func.attr
        for child in ast.walk(init_node)
        if isinstance(child, ast.Call) and isinstance(child.func, (ast.Name, ast.Attribute))
    }
    assert "ensure_runtime_schema" in bootstrap_source
    assert "bootstrap_runtime_schema" not in init_calls
    assert "ensure_runtime_schema" not in init_calls
