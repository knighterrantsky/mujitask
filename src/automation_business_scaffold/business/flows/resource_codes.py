from __future__ import annotations

from typing import Any

DEFAULT_BROWSER_RESOURCE_CODE = "browser.tiktok.main"


def build_browser_resource_code(params: dict[str, Any]) -> str:
    profile_ref = str(params.get("profile_ref", "") or "").strip()
    if not profile_ref:
        return DEFAULT_BROWSER_RESOURCE_CODE
    safe_profile = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "-"
        for character in profile_ref
    ).strip("-")
    return f"browser.tiktok.{safe_profile or 'main'}"
