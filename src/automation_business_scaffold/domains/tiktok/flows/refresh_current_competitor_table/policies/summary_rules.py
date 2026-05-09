from __future__ import annotations


def terminal_row_statuses() -> tuple[str, ...]:
    return ("success", "partial_success", "failed", "unavailable", "skipped")
