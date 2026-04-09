from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

try:
    import automation_framework.browser as _browser
except ModuleNotFoundError:  # pragma: no cover - dependency is present in production envs.
    _browser = None


@dataclass(slots=True)
class _FallbackBlockedContext:
    page_url: str = ""
    blocker_type: str = ""
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _FallbackBlockedHandlingConfig:
    handler: Callable[[Any, Any], Any] | None = None


@dataclass(slots=True)
class _FallbackBlockedResolution:
    action: str
    reason: str = ""

    @classmethod
    def resume_default(cls) -> "_FallbackBlockedResolution":
        return cls(action="resume_default")

    @classmethod
    def handled_recheck(cls, reason: str = "") -> "_FallbackBlockedResolution":
        return cls(action="handled_recheck", reason=reason)

    @classmethod
    def force_continue(cls, reason: str = "") -> "_FallbackBlockedResolution":
        return cls(action="force_continue", reason=reason)


@dataclass(slots=True)
class _FallbackBlockerRule:
    domains: list[str] = field(default_factory=list)
    detect_selectors: list[str] = field(default_factory=list)
    dismiss_selectors: list[str] = field(default_factory=list)
    dismiss_keywords: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _FallbackBlockerRulesConfig:
    inherit_defaults: bool = True
    domain_rules: list[_FallbackBlockerRule] = field(default_factory=list)


BlockedContext = getattr(_browser, "BlockedContext", _FallbackBlockedContext)
BlockedHandlingConfig = getattr(_browser, "BlockedHandlingConfig", _FallbackBlockedHandlingConfig)
BlockedResolution = getattr(_browser, "BlockedResolution", _FallbackBlockedResolution)
BlockerRule = getattr(_browser, "BlockerRule", _FallbackBlockerRule)
BlockerRulesConfig = getattr(_browser, "BlockerRulesConfig", _FallbackBlockerRulesConfig)

