from __future__ import annotations

import os
from dataclasses import dataclass


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class BusinessDefaults:
    default_run_mode: str
    source_system: str
    target_system: str
    default_category: str
    default_price: int
    default_description: str


def get_business_defaults() -> BusinessDefaults:
    return BusinessDefaults(
        default_run_mode=os.getenv("BUSINESS_DEFAULT_RUN_MODE", "draft"),
        source_system=os.getenv("BUSINESS_SOURCE_SYSTEM", "source-marketplace"),
        target_system=os.getenv("BUSINESS_TARGET_SYSTEM", "target-marketplace"),
        default_category=os.getenv("BUSINESS_DEFAULT_CATEGORY", "home"),
        default_price=_read_int("BUSINESS_DEFAULT_PRICE", 128),
        default_description=os.getenv(
            "BUSINESS_DEFAULT_DESCRIPTION",
            "Created from automation-business-scaffold.",
        ),
    )

