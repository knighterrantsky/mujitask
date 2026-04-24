"""Stable handler contracts, allowlists, and runtime registry primitives."""

from .allowlist import (
    ALLOWED_HANDLER_CODES,
    API_HANDLER_CODES,
    API_HANDLER_CONTRACTS,
    BROWSER_HANDLER_CODES,
    BROWSER_HANDLER_CONTRACTS,
    OUTBOX_HANDLER_CODES,
    OUTBOX_HANDLER_CONTRACTS,
    prohibited_handler_code_reason,
)
from .contract import (
    HandlerCallable,
    HandlerContext,
    HandlerContract,
    HandlerError,
    HandlerNextAction,
    HandlerResult,
    HandlerRuntimeTable,
    HandlerStatus,
    HandlerWorkerType,
)
from .registry import (
    DisallowedHandlerCodeError,
    HandlerInvocationContractError,
    HandlerNotBoundError,
    HandlerRegistry,
    HandlerRegistryError,
    RegisteredHandler,
    UnknownHandlerCodeError,
)

__all__ = [
    "ALLOWED_HANDLER_CODES",
    "API_HANDLER_CODES",
    "API_HANDLER_CONTRACTS",
    "BROWSER_HANDLER_CODES",
    "BROWSER_HANDLER_CONTRACTS",
    "DisallowedHandlerCodeError",
    "HandlerCallable",
    "HandlerContext",
    "HandlerContract",
    "HandlerError",
    "HandlerInvocationContractError",
    "HandlerNextAction",
    "HandlerNotBoundError",
    "HandlerRegistry",
    "HandlerRegistryError",
    "HandlerResult",
    "HandlerRuntimeTable",
    "HandlerStatus",
    "HandlerWorkerType",
    "OUTBOX_HANDLER_CODES",
    "OUTBOX_HANDLER_CONTRACTS",
    "RegisteredHandler",
    "UnknownHandlerCodeError",
    "prohibited_handler_code_reason",
]
