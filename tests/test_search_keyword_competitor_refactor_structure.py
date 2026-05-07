from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "automation_business_scaffold"
COMPETITOR_FLOW_PACKAGE = PACKAGE_ROOT / "domains" / "tiktok" / "flows" / "search_keyword_competitor_products"
COMPETITOR_TASK = PACKAGE_ROOT / "domains" / "tiktok" / "tasks" / "search_keyword_competitor_products.py"
COMPETITOR_WORKFLOW = PACKAGE_ROOT / "domains" / "tiktok" / "workflows" / "search_keyword_competitor_products.py"
COMPETITOR_ORCHESTRATOR = COMPETITOR_FLOW_PACKAGE / "orchestrator.py"
COMPETITOR_SUMMARY = COMPETITOR_FLOW_PACKAGE / "summary.py"

STAGE_CODES = (
    "keyword_seed_import",
    "fastmoss_security_browser_fallback",
    "dispatch_row_refresh_jobs",
    "refresh_competitor_rows",
    "browser_fallback",
    "resume_competitor_rows_after_browser_fallback",
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
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_competitor_keyword_flow_is_stage_oriented_package() -> None:
    assert COMPETITOR_FLOW_PACKAGE.is_dir()
    assert not COMPETITOR_FLOW_PACKAGE.with_suffix(".py").exists()
    assert (COMPETITOR_FLOW_PACKAGE / "__init__.py").is_file()
    assert (COMPETITOR_FLOW_PACKAGE / "context").is_dir()
    assert not (COMPETITOR_FLOW_PACKAGE / "context.py").exists()
    assert (COMPETITOR_FLOW_PACKAGE / "errors.py").is_file()
    assert COMPETITOR_ORCHESTRATOR.is_file()
    assert COMPETITOR_SUMMARY.is_file()

    actual_stage_modules = {
        path.stem
        for path in (COMPETITOR_FLOW_PACKAGE / "stages").glob("*.py")
        if path.name != "__init__.py"
    }
    assert actual_stage_modules == set(STAGE_CODES)
    for stage_code in STAGE_CODES:
        stage_source = _read(COMPETITOR_FLOW_PACKAGE / "stages" / f"{stage_code}.py")
        assert f'STAGE_CODE = "{stage_code}"' in stage_source
        assert "def advance(" in stage_source


def test_competitor_stage_modules_own_stage_logic() -> None:
    orchestrator_source = _read(COMPETITOR_ORCHESTRATOR)
    assert "def _advance_" not in orchestrator_source
    assert len(orchestrator_source.splitlines()) <= 180

    for stage_code in STAGE_CODES:
        stage_path = COMPETITOR_FLOW_PACKAGE / "stages" / f"{stage_code}.py"
        stage_source = _read(stage_path)
        assert "import orchestrator" not in stage_source
        assert "orchestrator._advance" not in stage_source
        assert "return _advance_" not in stage_source

    for context_module in (COMPETITOR_FLOW_PACKAGE / "context").glob("*.py"):
        assert "def _advance_" not in _read(context_module)


def test_competitor_summary_owns_final_assembly() -> None:
    summary_source = _read(COMPETITOR_SUMMARY)
    orchestrator_source = _read(COMPETITOR_ORCHESTRATOR)

    assert "def finalize_request(" in summary_source
    assert "build_outbox_message_text" in summary_source
    assert "create_notification_outbox" in summary_source
    assert "summary = {" in summary_source
    assert "create_notification_outbox" not in orchestrator_source


def test_competitor_task_and_workflow_shells_remain_thin() -> None:
    task_source = _read(COMPETITOR_TASK)
    workflow_source = _read(COMPETITOR_WORKFLOW)

    assert len(task_source.splitlines()) <= 80
    assert "run_search_keyword_competitor_products_request" in task_source
    assert "StageDefinition(" in workflow_source
    assert "FeishuBitableClient" not in workflow_source
    assert "FastMossClient" not in workflow_source
    assert "create_engine" not in workflow_source


def test_competitor_package_has_policy_modules_but_no_shared_kernel() -> None:
    policy_files = {
        path.name
        for path in (COMPETITOR_FLOW_PACKAGE / "policies").glob("*.py")
        if path.name != "__init__.py"
    }
    assert policy_files == {"candidate_filter.py", "dedupe.py", "fallback.py", "resume.py"}

    forbidden_paths = (
        PACKAGE_ROOT / "domains" / "tiktok" / "flows" / "keyword_shared",
        PACKAGE_ROOT / "domains" / "tiktok" / "shared",
    )
    assert [path for path in forbidden_paths if path.exists()] == []


def test_competitor_package_has_no_generic_dumping_ground_modules() -> None:
    forbidden_names = {"utils.py", "helper.py", "helpers.py", "common.py", "manager.py", "service.py"}
    found = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in _python_sources(COMPETITOR_FLOW_PACKAGE)
        if path.name in forbidden_names
    ]
    assert found == []


def test_competitor_flow_package_does_not_import_transport_or_runtime_repositories() -> None:
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
    for path in _python_sources(COMPETITOR_FLOW_PACKAGE):
        for imported in _imports(path):
            if imported.startswith(forbidden_import_prefixes):
                violations.append(f"{path.relative_to(REPO_ROOT)} imports {imported}")
    assert violations == []


def test_competitor_stages_do_not_import_sibling_workflow_packages() -> None:
    sibling_flow_prefix = "automation_business_scaffold.domains.tiktok.flows.search_keyword_selection_products"
    violations: list[str] = []
    for path in _python_sources(COMPETITOR_FLOW_PACKAGE / "stages"):
        for imported in _imports(path):
            if imported.startswith(sibling_flow_prefix):
                violations.append(f"{path.relative_to(REPO_ROOT)} imports {imported}")
    assert violations == []
