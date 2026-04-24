"""Handler contract facade."""

from automation_business_scaffold.business.handlers.contract import (
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
from automation_business_scaffold.business.handlers.registry import (
    DisallowedHandlerCodeError,
    HandlerInvocationContractError,
    HandlerNotBoundError,
    HandlerRegistry,
    HandlerRegistryError,
    RegisteredHandler,
    UnknownHandlerCodeError,
)

__all__ = [
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
    "RegisteredHandler",
    "UnknownHandlerCodeError",
]
