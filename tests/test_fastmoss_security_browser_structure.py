from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BROWSER_ROOT = REPO_ROOT / "src" / "automation_business_scaffold" / "capabilities" / "browser"
HANDLER = BROWSER_ROOT / "fastmoss_security_resolve_handler.py"
MECHANISM_ROOT = BROWSER_ROOT / "fastmoss_security"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_fastmoss_security_handler_stays_thin_facade() -> None:
    source = _read(HANDLER)

    forbidden_defs = (
        "def _bootstrap_fastmoss_login_cookies(",
        "def _import_fastmoss_browser_cookies(",
        "def _export_fastmoss_browser_cookies(",
        "def _verify_original_request_with_cookies(",
        "def _verify_original_request_with_cookies_result(",
        "def _save_browser_cookies_to_cache(",
        "def _cookie_snapshot_from_browser_cookies(",
        "def _resolve_browser_cookie_expires_at(",
        "def _try_resolve_fastmoss_slider_security_check(",
        "def _resolve_fastmoss_slider_with_framework_captcha(",
        "def _resolve_one_fastmoss_mixed_slider_attempt(",
        "def _build_fastmoss_mixed_slider_mapping(",
        "def _capture_fastmoss_browser_diagnostic_artifacts(",
        "def _wait_for_fastmoss_slider_state(",
        "def _wait_for_fastmoss_slider_elements(",
        "def _load_fastmoss_browser_image_resource(",
    )

    missing_modules = [
        relative
        for relative in (
            "element_state.py",
            "slider_challenge.py",
            "coordinate_mapping.py",
            "diagnostics.py",
            "session_bootstrap.py",
            "cookie_bridge.py",
            "request_verification.py",
            "cookie_cache_persistence.py",
        )
        if not (MECHANISM_ROOT / relative).is_file()
    ]
    assert missing_modules == []
    assert all(token not in source for token in forbidden_defs)
    assert "FastMossHTTPSession" not in source
    assert "RuntimeStore" not in source
    assert "attach_fastmoss_cookie_cache" not in source
    assert "build_fastmoss_cookie_cache_context" not in source
    assert "save_fastmoss_cookie_cache" not in source
    assert source.count("\ndef ") <= 20


def test_fastmoss_security_mechanism_modules_keep_browser_boundary() -> None:
    sources = "\n".join(_read(path) for path in MECHANISM_ROOT.glob("*.py"))

    assert "automation_business_scaffold.domains.tiktok" not in sources
    assert "automation_business_scaffold.control_plane" not in sources
    assert not (MECHANISM_ROOT / "__init__.py").exists()


def test_fastmoss_specific_mechanisms_do_not_move_into_page_primitives() -> None:
    source = _read(BROWSER_ROOT / "page_primitives.py")

    forbidden_tokens = (
        "FastMoss",
        "fastmoss",
        "FASTMOSS",
        "tcaptcha",
        "MSG_SAFE_0001",
        "slider_captcha_audit",
    )

    assert all(token not in source for token in forbidden_tokens)
