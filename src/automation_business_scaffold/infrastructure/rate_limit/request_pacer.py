from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, Callable

DEFAULT_API_REQUEST_MIN_DELAY_SECONDS = 0.5
DEFAULT_API_REQUEST_MAX_DELAY_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class RequestPacerConfig:
    min_delay_seconds: float = 0.0
    max_delay_seconds: float = 0.0


class RequestPacer:
    """Apply per-key randomized delay between transport requests."""

    def __init__(
        self,
        config: RequestPacerConfig | None = None,
        *,
        sleep_factory: Callable[[float], None] = time.sleep,
        monotonic_factory: Callable[[], float] = time.monotonic,
        random_uniform: Callable[[float, float], float] = random.uniform,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config or RequestPacerConfig()
        self._sleep_factory = sleep_factory
        self._monotonic_factory = monotonic_factory
        self._random_uniform = random_uniform
        self._event_callback = event_callback
        self._last_finished_at_by_key: dict[str, float] = {}

    def wait_before_request(self, key: str = "default") -> float:
        key = str(key or "default")
        if key not in self._last_finished_at_by_key:
            self._emit_event("request_pacer_ready", key=key, delay_seconds=0.0, request_started_at=time.time())
            return 0.0
        min_delay, max_delay = _normalize_delay_range(
            self.config.min_delay_seconds,
            self.config.max_delay_seconds,
        )
        if max_delay <= 0:
            self._emit_event("request_pacer_ready", key=key, delay_seconds=0.0, request_started_at=time.time())
            return 0.0
        delay_seconds = self._random_uniform(min_delay, max_delay)
        if delay_seconds > 0:
            self._emit_event("request_pacer_sleep", key=key, delay_seconds=delay_seconds)
            self._sleep_factory(delay_seconds)
        self._emit_event("request_pacer_ready", key=key, delay_seconds=delay_seconds, request_started_at=time.time())
        return delay_seconds

    def mark_request_finished(self, key: str = "default") -> None:
        key = str(key or "default")
        self._last_finished_at_by_key[key] = self._monotonic_factory()
        self._emit_event("request_pacer_finished", key=key, request_finished_at=time.time())

    def _emit_event(self, kind: str, **payload: Any) -> None:
        if self._event_callback is None:
            return
        event = {"kind": kind, "ts_ms": int(time.time() * 1000), **payload}
        try:
            self._event_callback(event)
        except Exception:
            return


def _normalize_delay_range(min_delay: float, max_delay: float) -> tuple[float, float]:
    min_delay = max(0.0, float(min_delay or 0.0))
    max_delay = max(0.0, float(max_delay or 0.0))
    if max_delay < min_delay:
        min_delay, max_delay = max_delay, min_delay
    return min_delay, max_delay


def resolve_api_request_pacer_config(
    settings: Mapping[str, Any] | None = None,
    *,
    provider: str = "",
) -> RequestPacerConfig:
    min_delay, max_delay = resolve_api_request_delay_range(settings, provider=provider)
    return RequestPacerConfig(min_delay_seconds=min_delay, max_delay_seconds=max_delay)


def resolve_api_request_delay_range(
    settings: Mapping[str, Any] | None = None,
    *,
    provider: str = "",
) -> tuple[float, float]:
    mapping = dict(settings or {})
    provider_key = _provider_key(provider)
    min_value = _first_config_value(
        mapping,
        (
            f"{provider_key}_api_request_delay_min_seconds",
            f"{provider_key}_request_delay_min_seconds",
            "api_request_delay_min_seconds",
            "request_delay_min_seconds",
        ),
        env_names=_provider_env_names(provider_key, "MIN") + ("MUJITASK_API_REQUEST_MIN_DELAY_SECONDS",),
        default=DEFAULT_API_REQUEST_MIN_DELAY_SECONDS,
    )
    max_value = _first_config_value(
        mapping,
        (
            f"{provider_key}_api_request_delay_max_seconds",
            f"{provider_key}_request_delay_max_seconds",
            "api_request_delay_max_seconds",
            "request_delay_max_seconds",
        ),
        env_names=_provider_env_names(provider_key, "MAX") + ("MUJITASK_API_REQUEST_MAX_DELAY_SECONDS",),
        default=DEFAULT_API_REQUEST_MAX_DELAY_SECONDS,
    )
    return _normalize_delay_range(_float_or_default(min_value, DEFAULT_API_REQUEST_MIN_DELAY_SECONDS), _float_or_default(max_value, DEFAULT_API_REQUEST_MAX_DELAY_SECONDS))


def _first_config_value(
    settings: Mapping[str, Any],
    keys: tuple[str, ...],
    *,
    env_names: tuple[str, ...],
    default: float,
) -> Any:
    for key in keys:
        if key and settings.get(key) not in (None, ""):
            return settings[key]
    for env_name in env_names:
        value = os.environ.get(env_name)
        if value not in (None, ""):
            return value
    return default


def _provider_key(provider: str) -> str:
    return str(provider or "").strip().lower().replace("-", "_")


def _provider_env_names(provider_key: str, bound: str) -> tuple[str, ...]:
    if not provider_key:
        return ()
    env_provider = provider_key.upper()
    return (f"MUJITASK_{env_provider}_API_REQUEST_{bound}_DELAY_SECONDS",)


def _float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
