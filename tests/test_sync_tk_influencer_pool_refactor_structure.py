from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FLOW_ROOT = (
    REPO_ROOT
    / "src"
    / "automation_business_scaffold"
    / "domains"
    / "tiktok"
    / "flows"
    / "sync_tk_influencer_pool"
)

STAGE_CODES = {
    "read_competitor_candidates",
    "dispatch_product_jobs",
    "discover_related_creators",
    "fastmoss_security_browser_fallback",
    "sync_influencer_pool",
    "collect_creator_detail",
    "persist_creator_facts",
    "write_influencer_pool",
    "finalize_product",
    "writeback_competitor_status",
    "ready_for_summary",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _stage_modules() -> set[str]:
    return {
        path.stem
        for path in (FLOW_ROOT / "stages").glob("*.py")
        if path.name != "__init__.py"
    }


def test_sync_tk_influencer_pool_is_package_flow() -> None:
    assert FLOW_ROOT.is_dir()
    assert not FLOW_ROOT.with_suffix(".py").exists()
    for filename in ("__init__.py", "orchestrator.py", "errors.py", "summary.py"):
        assert (FLOW_ROOT / filename).is_file()
    assert (FLOW_ROOT / "context").is_dir()
    assert not (FLOW_ROOT / "context.py").exists()
    assert _stage_modules() == STAGE_CODES


def test_sync_orchestrator_is_dispatch_glue_not_stage_owner() -> None:
    source = _read(FLOW_ROOT / "orchestrator.py")
    assert "import_module" in source
    assert "def _advance_" not in source
    assert "def _build_" not in source
    assert len(source.splitlines()) <= 150


def test_sync_summary_owns_final_assembly() -> None:
    source = _read(FLOW_ROOT / "summary.py")
    assert "def finalize_request" in source
    assert "def finalize_sync_tk_influencer_pool_request" in source
    assert "summary_payload" in source
    assert "create_notification_outbox" in source


def test_sync_stage_modules_own_stage_logic() -> None:
    for stage_code in STAGE_CODES - {"ready_for_summary"}:
        source = _read(FLOW_ROOT / "stages" / f"{stage_code}.py")
        assert f'STAGE_CODE = "{stage_code}"' in source
        assert "def advance(" in source
        assert "def _advance_stage_" in source
        assert "orchestrator" not in source


def test_sync_package_has_no_dumping_ground_or_shared_kernel() -> None:
    forbidden_names = {"utils.py", "helpers.py", "common.py", "shared.py"}
    assert forbidden_names.isdisjoint({path.name for path in FLOW_ROOT.rglob("*.py")})
    forbidden_dirs = {"shared", "row_shared", "keyword_shared"}
    assert forbidden_dirs.isdisjoint({path.name for path in FLOW_ROOT.rglob("*") if path.is_dir()})
