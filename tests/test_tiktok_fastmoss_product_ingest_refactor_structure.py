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
    / "tiktok_fastmoss_product_ingest"
)

STAGE_CODES = {
    "read_selection_rows",
    "dispatch_selection_row_refresh",
    "collect_selection_rows",
    "selection_row_browser_fallback",
    "resume_selection_rows_after_browser_fallback",
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


def test_tiktok_fastmoss_product_ingest_is_package_flow() -> None:
    assert FLOW_ROOT.is_dir()
    assert not FLOW_ROOT.with_suffix(".py").exists()
    for filename in ("__init__.py", "orchestrator.py", "context.py", "errors.py", "summary.py"):
        assert (FLOW_ROOT / filename).is_file()
    assert _stage_modules() == STAGE_CODES


def test_ingest_orchestrator_is_dispatch_glue_not_stage_owner() -> None:
    source = _read(FLOW_ROOT / "orchestrator.py")
    assert "import_module" in source
    assert "def _advance_" not in source
    assert "def _build_" not in source
    assert len(source.splitlines()) <= 140


def test_ingest_summary_owns_final_assembly() -> None:
    source = _read(FLOW_ROOT / "summary.py")
    assert "def finalize_request" in source
    assert "row_results" in source
    assert "create_notification_outbox" in source
    assert "build_outbox_message_text" in source


def test_ingest_stage_modules_own_stage_logic() -> None:
    for stage_code in STAGE_CODES - {"ready_for_summary"}:
        source = _read(FLOW_ROOT / "stages" / f"{stage_code}.py")
        assert f'STAGE_CODE = "{stage_code}"' in source
        assert "def advance(" in source
        assert "def _advance_" in source
        assert "orchestrator" not in source


def test_ingest_package_has_no_dumping_ground_or_shared_kernel() -> None:
    forbidden_names = {"utils.py", "helpers.py", "common.py", "shared.py"}
    assert forbidden_names.isdisjoint({path.name for path in FLOW_ROOT.rglob("*.py")})
    forbidden_dirs = {"shared", "row_shared", "keyword_shared"}
    assert forbidden_dirs.isdisjoint({path.name for path in FLOW_ROOT.rglob("*") if path.is_dir()})

