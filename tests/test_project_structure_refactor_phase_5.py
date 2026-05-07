from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FLOWS = REPO_ROOT / "src" / "automation_business_scaffold" / "domains" / "tiktok" / "flows"
PACKAGE_REFACTORED_FLOWS = (
    "search_keyword_selection_products",
    "search_keyword_competitor_products",
    "refresh_current_competitor_table",
    "sync_tk_influencer_pool",
    "tiktok_fastmoss_product_ingest",
)
PHASE_4_CONTEXT_MODULES = {
    "__init__.py",
    "models.py",
    "runtime_views.py",
    "stage_inputs.py",
    "decision_models.py",
    "summary_inputs.py",
}
LEAF_ROW_FLOWS = ("selection_row_refresh", "competitor_row_refresh")
LEAF_CONTEXT_MODULES = {
    "__init__.py",
    "models.py",
    "runtime_views.py",
    "pipeline_inputs.py",
    "decision_models.py",
    "summary_inputs.py",
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


def test_phase_4_context_deflation_flows_remain_package_structured() -> None:
    for flow_name in PACKAGE_REFACTORED_FLOWS:
        flow_package = FLOWS / flow_name
        context_package = flow_package / "context"

        assert flow_package.is_dir(), flow_name
        assert not flow_package.with_suffix(".py").exists(), flow_name
        assert context_package.is_dir(), flow_name
        assert {path.name for path in context_package.glob("*.py")} >= PHASE_4_CONTEXT_MODULES


def test_leaf_row_flows_are_package_oriented() -> None:
    for flow_name in LEAF_ROW_FLOWS:
        flow_package = FLOWS / flow_name

        assert flow_package.is_dir(), flow_name
        assert not flow_package.with_suffix(".py").exists(), flow_name
        assert (flow_package / "orchestrator.py").is_file(), flow_name
        assert (flow_package / "summary.py").is_file(), flow_name
        assert (flow_package / "errors.py").is_file(), flow_name
        assert (flow_package / "pipeline").is_dir(), flow_name
        assert (flow_package / "policies").is_dir(), flow_name
        assert {path.name for path in (flow_package / "context").glob("*.py")} >= LEAF_CONTEXT_MODULES


def test_new_leaf_package_init_files_are_not_export_surfaces() -> None:
    for flow_name in LEAF_ROW_FLOWS:
        for package in (
            FLOWS / flow_name,
            FLOWS / flow_name / "context",
            FLOWS / flow_name / "pipeline",
            FLOWS / flow_name / "policies",
        ):
            init_file = package / "__init__.py"
            assert init_file.is_file(), package
            assert _is_empty_or_docstring_only(init_file), init_file.relative_to(REPO_ROOT)
