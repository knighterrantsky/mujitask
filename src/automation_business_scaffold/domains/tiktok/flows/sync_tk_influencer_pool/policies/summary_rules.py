from __future__ import annotations


def product_group_terminal_statuses() -> tuple[str, ...]:
    return ("success", "partial_success", "failed", "skipped")
