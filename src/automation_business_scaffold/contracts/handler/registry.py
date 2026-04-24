from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .allowlist import prohibited_handler_code_reason
from .contract import (
    HandlerCallable,
    HandlerContext,
    HandlerContract,
    HandlerResult,
    HandlerRuntimeTable,
    HandlerWorkerType,
)


class HandlerRegistryError(Exception):
    """Base exception for handler registry failures."""


class DisallowedHandlerCodeError(HandlerRegistryError):
    """Raised when a disallowed legacy or workflow name reaches the registry."""


class UnknownHandlerCodeError(HandlerRegistryError):
    """Raised when a code is not admitted to the current registry lane."""


class HandlerNotBoundError(HandlerRegistryError):
    """Raised when lookup succeeds but no implementation has been attached yet."""


class HandlerInvocationContractError(HandlerRegistryError):
    """Raised when runtime context or handler output violates the shared contract."""


@dataclass
class RegisteredHandler:
    contract: HandlerContract
    handler: HandlerCallable | None = None

    @property
    def handler_code(self) -> str:
        return self.contract.handler_code

    @property
    def is_bound(self) -> bool:
        return self.handler is not None

    def bind(self, handler: HandlerCallable, *, replace: bool = False) -> RegisteredHandler:
        if self.handler is not None and not replace:
            raise HandlerRegistryError(
                f"handler {self.handler_code!r} is already bound; pass replace=True to rebind it."
            )
        self.handler = handler
        return self

    def invoke(self, context: HandlerContext) -> HandlerResult:
        try:
            self.contract.validate_context(context)
        except ValueError as exc:
            raise HandlerInvocationContractError(str(exc)) from exc
        if self.handler is None:
            raise HandlerNotBoundError(
                f"handler {self.handler_code!r} is allowlisted but not bound to an implementation yet."
            )
        result = self.handler(context)
        if result.handler_code != self.handler_code:
            raise HandlerInvocationContractError(
                f"handler {self.handler_code!r} returned envelope for {result.handler_code!r}."
            )
        if result.request_id != context.request_id:
            raise HandlerInvocationContractError(
                f"handler {self.handler_code!r} returned request_id {result.request_id!r}, "
                f"expected {context.request_id!r}."
            )
        if result.job_id != context.job_id:
            raise HandlerInvocationContractError(
                f"handler {self.handler_code!r} returned job_id {result.job_id!r}, "
                f"expected {context.job_id!r}."
            )
        return result


class HandlerRegistry:
    def __init__(
        self,
        *,
        registry_name: str,
        worker_type: HandlerWorkerType,
        runtime_table: HandlerRuntimeTable,
        allowed_contracts: Mapping[str, HandlerContract],
    ) -> None:
        self.registry_name = registry_name
        self.worker_type = worker_type
        self.runtime_table = runtime_table
        self._allowed_contracts = dict(allowed_contracts)
        self._handlers = {
            handler_code: RegisteredHandler(contract=contract)
            for handler_code, contract in self._allowed_contracts.items()
        }

    @property
    def allowed_codes(self) -> frozenset[str]:
        return frozenset(self._allowed_contracts)

    def _validate_handler_code(self, handler_code: str) -> None:
        prohibited_reason = prohibited_handler_code_reason(handler_code)
        if prohibited_reason is not None:
            raise DisallowedHandlerCodeError(
                f"handler_code {handler_code!r} is rejected: {prohibited_reason}"
            )
        if handler_code not in self._allowed_contracts:
            raise UnknownHandlerCodeError(
                f"handler_code {handler_code!r} is not admitted to the {self.registry_name} registry."
            )

    def get_contract(self, handler_code: str) -> HandlerContract:
        self._validate_handler_code(handler_code)
        return self._allowed_contracts[handler_code]

    def register_placeholder(self, handler_code: str) -> RegisteredHandler:
        self._validate_handler_code(handler_code)
        return self._handlers[handler_code]

    def bind(
        self,
        handler_code: str,
        handler: HandlerCallable,
        *,
        replace: bool = False,
    ) -> RegisteredHandler:
        entry = self.register_placeholder(handler_code)
        if entry.contract.worker_type != self.worker_type:
            raise HandlerRegistryError(
                f"handler {handler_code!r} belongs to {entry.contract.worker_type!r}, "
                f"cannot bind it into {self.worker_type!r}."
            )
        if entry.contract.runtime_table != self.runtime_table:
            raise HandlerRegistryError(
                f"handler {handler_code!r} belongs to {entry.contract.runtime_table!r}, "
                f"cannot bind it into {self.runtime_table!r}."
            )
        return entry.bind(handler, replace=replace)

    def get(self, handler_code: str) -> RegisteredHandler:
        self._validate_handler_code(handler_code)
        return self._handlers[handler_code]

    def lookup(self, handler_code: str) -> RegisteredHandler | None:
        prohibited_reason = prohibited_handler_code_reason(handler_code)
        if prohibited_reason is not None:
            raise DisallowedHandlerCodeError(
                f"handler_code {handler_code!r} is rejected: {prohibited_reason}"
            )
        return self._handlers.get(handler_code)

    def dispatch(self, handler_code: str, context: HandlerContext) -> HandlerResult:
        return self.get(handler_code).invoke(context)

    def entries(self) -> tuple[RegisteredHandler, ...]:
        return tuple(self._handlers[handler_code] for handler_code in sorted(self._handlers))
