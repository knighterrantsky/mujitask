from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FLOWS = REPO_ROOT / "src" / "automation_business_scaffold" / "domains" / "tiktok" / "flows"

FLOW_STAGE_CODES = {
    "search_keyword_selection_products": {
        "keyword_seed_import",
        "fastmoss_security_browser_fallback",
        "dispatch_selection_row_refresh_jobs",
        "refresh_selection_rows",
        "selection_row_browser_fallback",
        "resume_selection_rows_after_browser_fallback",
        "ready_for_summary",
    },
    "search_keyword_competitor_products": {
        "keyword_seed_import",
        "fastmoss_security_browser_fallback",
        "dispatch_row_refresh_jobs",
        "refresh_competitor_rows",
        "browser_fallback",
        "resume_competitor_rows_after_browser_fallback",
        "ready_for_summary",
    },
    "refresh_current_competitor_table": {
        "read_competitor_rows",
        "dispatch_product_collection",
        "collect_product_data",
        "browser_fallback",
        "resume_competitor_rows_after_browser_fallback",
        "ready_for_summary",
    },
    "sync_tk_influencer_pool": {
        "read_competitor_candidates",
        "dispatch_product_jobs",
        "discover_related_creators",
        "sync_influencer_pool",
        "writeback_competitor_status",
        "ready_for_summary",
    },
    "tiktok_fastmoss_product_ingest": {
        "read_selection_rows",
        "dispatch_selection_row_refresh",
        "collect_selection_rows",
        "selection_row_browser_fallback",
        "resume_selection_rows_after_browser_fallback",
        "ready_for_summary",
    },
}


def _stage_modules(flow_package: Path) -> set[str]:
    return {
        path.stem
        for path in (flow_package / "stages").glob("*.py")
        if path.name != "__init__.py"
    }


def test_all_formal_top_level_tiktok_flows_are_package_structured() -> None:
    for flow_name, stage_codes in FLOW_STAGE_CODES.items():
        flow_package = FLOWS / flow_name
        assert flow_package.is_dir(), flow_name
        assert not flow_package.with_suffix(".py").exists(), flow_name
        assert (flow_package / "orchestrator.py").is_file()
        assert (flow_package / "context.py").is_file()
        assert (flow_package / "errors.py").is_file()
        assert (flow_package / "summary.py").is_file()
        assert _stage_modules(flow_package) == stage_codes


def test_phase_3_did_not_package_row_level_leaf_flows_or_create_shared_kernel() -> None:
    assert (FLOWS / "selection_row_refresh.py").is_file()
    assert (FLOWS / "competitor_row_refresh.py").is_file()
    assert not (FLOWS / "shared").exists()
    assert not (FLOWS / "keyword_shared").exists()
    assert not (REPO_ROOT / "src" / "automation_business_scaffold" / "domains" / "tiktok" / "shared").exists()
    assert not (FLOWS / "row_shared").exists()

