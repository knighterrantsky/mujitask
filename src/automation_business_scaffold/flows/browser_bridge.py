from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from automation_framework.browser import (
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


@contextmanager
def open_automation_page(
    *,
    profile_ref: str | None = None,
    workspace_id: int | None = None,
    profile_id: str | None = None,
    provider_name: str | None = None,
    headless: bool = False,
    force_open: bool = False,
) -> Iterator[BrowserPageSession]:
    target = resolve_browser_target(
        profile_ref=profile_ref,
        workspace_id=workspace_id,
        profile_id=profile_id,
        provider_name=provider_name,
    )
    provider = build_browser_provider(target.provider)
    request = BrowserSessionRequest(
        profile_id=target.profile_id,
        workspace_id=target.workspace_id,
        headless=headless,
        force_open=force_open,
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
