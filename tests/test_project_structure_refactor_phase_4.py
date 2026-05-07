from __future__ import annotations

from pathlib import Path
import ast


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "automation_business_scaffold"
FLOWS = PACKAGE_ROOT / "domains" / "tiktok" / "flows"

PACKAGE_REFACTORED_FLOWS = (
    "search_keyword_selection_products",
    "search_keyword_competitor_products",
    "refresh_current_competitor_table",
    "sync_tk_influencer_pool",
    "tiktok_fastmoss_product_ingest",
)
REQUIRED_CONTEXT_MODULES = {
    "__init__.py",
    "models.py",
    "runtime_views.py",
    "stage_inputs.py",
    "decision_models.py",
    "summary_inputs.py",
}


def test_package_refactored_top_level_flows_have_decomposed_context_packages() -> None:
    for flow_name in PACKAGE_REFACTORED_FLOWS:
        flow_package = FLOWS / flow_name
        context_package = flow_package / "context"

        assert flow_package.is_dir(), flow_name
        assert not (flow_package / "context.py").exists(), flow_name
        assert context_package.is_dir(), flow_name
        assert {path.name for path in context_package.glob("*.py")} >= REQUIRED_CONTEXT_MODULES


def test_flow_package_boundaries_still_hold() -> None:
    for flow_name in PACKAGE_REFACTORED_FLOWS:
        flow_package = FLOWS / flow_name

        assert (flow_package / "orchestrator.py").is_file(), flow_name
        assert (flow_package / "summary.py").is_file(), flow_name
        assert (flow_package / "stages").is_dir(), flow_name
        assert not (FLOWS / f"{flow_name}.py").exists(), flow_name


def test_package_refactored_top_level_flow_init_files_are_not_export_surfaces() -> None:
    for flow_name in PACKAGE_REFACTORED_FLOWS:
        init_file = FLOWS / flow_name / "__init__.py"
        source = init_file.read_text(encoding="utf-8").strip()
        if not source:
            continue
        tree = ast.parse(source, filename=str(init_file))
        assert (
            len(tree.body) == 1
            and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ), flow_name
