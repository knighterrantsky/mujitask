from __future__ import annotations

from ..allowlist import API_HANDLER_CONTRACTS
from .implementations import feishu_table_write_handler

HANDLER_CODE = "feishu_table_write"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]

__all__ = ["CONTRACT", "HANDLER_CODE", "feishu_table_write_handler"]
