from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Callable


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
            return 0.0
        min_delay, max_delay = _normalize_delay_range(
            self.config.min_delay_seconds,
            self.config.max_delay_seconds,
        )
        if max_delay <= 0:
            return 0.0
        delay_seconds = self._random_uniform(min_delay, max_delay)
        if delay_seconds > 0:
            self._emit_event("request_pacer_sleep", key=key, delay_seconds=delay_seconds)
            self._sleep_factory(delay_seconds)
        return delay_seconds

    def mark_request_finished(self, key: str = "default") -> None:
        key = str(key or "default")
        self._last_finished_at_by_key[key] = self._monotonic_factory()

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
