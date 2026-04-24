"""Outbox-backed outbound channel capability."""

from .message_dispatch_handler import CONTRACT, HANDLER_CODE, outbox_dispatch_handler

__all__ = ["CONTRACT", "HANDLER_CODE", "outbox_dispatch_handler"]
