from __future__ import annotations

from ..context.models import DEFAULT_COMPETITOR_FILTER_SPEC


def default_row_filter() -> dict[str, object]:
    return dict(DEFAULT_COMPETITOR_FILTER_SPEC)
