from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FLOWS = REPO_ROOT / "src" / "automation_business_scaffold" / "domains" / "tiktok" / "flows"
FLOW_PACKAGE = FLOWS / "selection_row_refresh"

REQUIRED_CONTEXT_MODULES = {
    "__init__.py",
    "models.py",
    "runtime_views.py",
    "pipeline_inputs.py",
    "decision_models.py",
    "summary_inputs.py",
}
REQUIRED_PIPELINE_MODULES = {
    "__init__.py",
    "identity.py",
    "tiktok_request.py",
    "browser_fallback.py",
    "media_sync.py",
    "fastmoss_fetch.py",
    "fact_persistence.py",
    "row_writeback.py",
    "finalization.py",
}
FORBIDDEN_DUMPING_GROUND_NAMES = {
    "utils.py",
    "helper.py",
    "helpers.py",
    "common.py",
    "shared.py",
    "_implementations.py",
}


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


def test_selection_row_refresh_is_package_not_giant_file() -> None:
    assert FLOW_PACKAGE.is_dir()
    assert not (FLOWS / "selection_row_refresh.py").exists()
    assert (FLOW_PACKAGE / "__init__.py").is_file()
    assert _is_empty_or_docstring_only(FLOW_PACKAGE / "__init__.py")


def test_selection_row_refresh_package_shape() -> None:
    assert (FLOW_PACKAGE / "context").is_dir()
    assert (FLOW_PACKAGE / "errors.py").is_file()
    assert (FLOW_PACKAGE / "orchestrator.py").is_file()
    assert (FLOW_PACKAGE / "summary.py").is_file()
    assert (FLOW_PACKAGE / "pipeline").is_dir()
    assert (FLOW_PACKAGE / "policies").is_dir()
    assert {path.name for path in (FLOW_PACKAGE / "context").glob("*.py")} >= REQUIRED_CONTEXT_MODULES
    assert {path.name for path in (FLOW_PACKAGE / "pipeline").glob("*.py")} >= REQUIRED_PIPELINE_MODULES


def test_selection_row_refresh_has_no_dumping_ground_modules() -> None:
    found = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in FLOW_PACKAGE.rglob("*.py")
        if path.name in FORBIDDEN_DUMPING_GROUND_NAMES
    ]

    assert found == []


def test_selection_row_refresh_orchestrator_stays_thin() -> None:
    source = (FLOW_PACKAGE / "orchestrator.py").read_text(encoding="utf-8")
    assert len(source.splitlines()) <= 80
    assert "api_handler_callable" not in source
    assert "feishu_table_write_handler" not in source
