"""Handler contracts, concrete implementations, and registries for the rewrite architecture."""

from .api import (
    bind_default_api_handlers,
    build_api_handler_registry,
    build_bound_api_handler_registry,
    register_api_handler,
    register_api_placeholder,
)
from .allowlist import (
    ALLOWED_HANDLER_CODES,
    API_HANDLER_CODES,
    BROWSER_HANDLER_CODES,
    OUTBOX_HANDLER_CODES,
)
from .browser import (
    bind_default_browser_handlers,
    build_browser_handler_registry,
    build_bound_browser_handler_registry,
    register_browser_handler,
    register_browser_placeholder,
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
from .outbox import (
    bind_default_outbox_handlers,
    build_outbox_handler_registry,
    build_bound_outbox_handler_registry,
    register_outbox_handler,
    register_outbox_placeholder,
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
    "BROWSER_HANDLER_CODES",
    "DisallowedHandlerCodeError",
    "bind_default_api_handlers",
    "bind_default_browser_handlers",
    "bind_default_outbox_handlers",
    "build_api_handler_registry",
    "build_browser_handler_registry",
    "build_bound_api_handler_registry",
    "build_bound_browser_handler_registry",
    "build_bound_outbox_handler_registry",
    "build_outbox_handler_registry",
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
    "register_api_handler",
    "register_api_placeholder",
    "register_browser_handler",
    "register_browser_placeholder",
    "register_outbox_handler",
    "register_outbox_placeholder",
    "RegisteredHandler",
    "UnknownHandlerCodeError",
]
