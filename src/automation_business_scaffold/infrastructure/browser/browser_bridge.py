from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Iterator
from urllib.parse import urlparse

import requests
from automation_framework.browser import (
    BlockedHandlingConfig,
    BlockerRulesConfig,
    BrowserSessionRequest,
    build_browser_provider,
    build_target_key,
    resolve_browser_target,
)


@dataclass(slots=True)
class BrowserPageSession:
    provider_name: str
    target_key: str
    profile_ref: str
    session_ref: str
    humanize: bool
    page: Any
    raw_page: Any


def _metadata_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_debug_http(value: Any) -> str:
    text = str(value or "http://127.0.0.1:9222").strip().rstrip("/")
    if not text:
        return "http://127.0.0.1:9222"
    if text.startswith(("http://", "https://")):
        return text
    return f"http://{text}"


def _chrome_cdp_ready(debug_http: str) -> bool:
    try:
        response = requests.get(f"{debug_http}/json/version", timeout=2)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return False
    return bool(payload.get("Browser") or payload.get("webSocketDebuggerUrl"))


def _detect_chrome_bin(metadata: dict[str, Any]) -> str:
    candidates = [
        str(metadata.get("chrome_bin") or "").strip(),
        str(metadata.get("chrome_path") or "").strip(),
        os.getenv("MUJITASK_CHROME_BIN", "").strip(),
        os.getenv("GOOGLE_CHROME_BIN", "").strip(),
        os.getenv("CHROME_BIN", "").strip(),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/snap/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError(
        "Google Chrome was not found. Set MUJITASK_CHROME_BIN or metadata.chrome_bin "
        "for the chrome_cdp browser profile."
    )


def _profile_user_data_dir(*, metadata: dict[str, Any], profile_ref: str) -> Path:
    configured = (
        str(metadata.get("user_data_dir") or "").strip()
        or os.getenv("MUJITASK_CHROME_PROFILE_DIR", "").strip()
    )
    if configured:
        return Path(configured).expanduser()

    safe_profile = re.sub(r"[^A-Za-z0-9_.-]+", "-", profile_ref).strip("-") or "default"
    return Path.home() / ".mujitask" / "chrome-cdp" / safe_profile


def _metadata_launch_args(metadata: dict[str, Any]) -> list[str]:
    raw = metadata.get("launch_args") or metadata.get("chrome_args") or []
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _chrome_launch_command(
    *,
    chrome_bin: str,
    debug_http: str,
    user_data_dir: Path,
    metadata: dict[str, Any],
) -> list[str]:
    parsed = urlparse(debug_http)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 9222
    args = [
        f"--remote-debugging-port={port}",
        f"--remote-debugging-address={host}",
        f"--user-data-dir={user_data_dir}",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    seen = set(args)
    for item in _metadata_launch_args(metadata):
        if item not in seen:
            args.append(item)
            seen.add(item)
    return [chrome_bin, *args]


def _ensure_chrome_cdp_started(target: Any) -> None:
    if str(target.provider).strip().lower() != "chrome_cdp":
        return

    metadata = target.metadata if isinstance(target.metadata, dict) else {}
    if not _metadata_bool(metadata.get("auto_start")):
        return

    debug_http = _normalize_debug_http(metadata.get("debug_http") or metadata.get("cdp_url"))
    if _chrome_cdp_ready(debug_http):
        return

    user_data_dir = _profile_user_data_dir(metadata=metadata, profile_ref=str(target.profile_ref))
    user_data_dir.mkdir(parents=True, exist_ok=True)
    command = _chrome_launch_command(
        chrome_bin=_detect_chrome_bin(metadata),
        debug_http=debug_http,
        user_data_dir=user_data_dir,
        metadata=metadata,
    )
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _chrome_cdp_ready(debug_http):
            return
        time.sleep(0.25)


@contextmanager
def open_automation_page(
    *,
    profile_ref: str | None = None,
    workspace_id: int | None = None,
    profile_id: str | None = None,
    provider_name: str | None = None,
    headless: bool = False,
    force_open: bool = False,
    blocked_handling: BlockedHandlingConfig | None = None,
    blocker_rules: BlockerRulesConfig | None = None,
) -> Iterator[BrowserPageSession]:
    target = resolve_browser_target(
        profile_ref=profile_ref,
        workspace_id=workspace_id,
        profile_id=profile_id,
        provider_name=provider_name,
    )
    _ensure_chrome_cdp_started(target)
    provider = build_browser_provider(target.provider)
    request = BrowserSessionRequest(
        profile_id=target.profile_id,
        workspace_id=target.workspace_id,
        headless=headless,
        force_open=force_open,
        blocked_handling=blocked_handling or BlockedHandlingConfig(),
        blocker_rules=blocker_rules or BlockerRulesConfig(),
        metadata={
            "profile_ref": target.profile_ref,
            **target.metadata,
        },
    )
    session = provider.open_session(request)
    try:
        if hasattr(session, "get_or_create_automation_page"):
            page = session.get_or_create_automation_page()
            raw_page = getattr(page, "raw_page", page)
            yield BrowserPageSession(
                provider_name=provider.provider_name,
                target_key=build_target_key(target),
                profile_ref=target.profile_ref,
                session_ref=session.session_ref,
                humanize=bool(getattr(page, "humanize", False)),
                page=page,
                raw_page=raw_page,
            )
        else:
            page = session.get_or_create_page()
            raw_page = getattr(page, "raw_page", page)
            yield BrowserPageSession(
                provider_name=provider.provider_name,
                target_key=build_target_key(target),
                profile_ref=target.profile_ref,
                session_ref=session.session_ref,
                humanize=bool(getattr(page, "humanize", False)),
                page=page,
                raw_page=raw_page,
            )
    finally:
        session.close()
