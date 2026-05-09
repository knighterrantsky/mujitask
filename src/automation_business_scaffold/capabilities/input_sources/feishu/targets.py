from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FeishuTableTarget:
    access_token: str
    app_token: str
    table_id: str
    view_id: str = ""
    table_ref: str = ""
    table_url: str = ""


@dataclass(frozen=True)
class FeishuCommonError(Exception):
    error_type: str
    error_code: str
    message: str
    retryable: bool
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)
