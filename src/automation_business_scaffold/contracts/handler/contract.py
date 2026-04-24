from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

HandlerWorkerType = Literal["api_worker", "browser_worker", "outbox_dispatcher"]
HandlerRuntimeTable = Literal["api_worker_job", "task_execution", "notification_outbox"]
HandlerStatus = Literal["success", "skipped", "partial_success", "failed", "fallback_required"]


@dataclass(frozen=True)
class HandlerNextAction:
    type: str = "none"
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class HandlerError:
    error_type: str
    error_code: str
    message: str
    retryable: bool
    fallback_allowed: bool = False
    fallback_reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "error_code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
            "fallback_allowed": self.fallback_allowed,
            "fallback_reason": self.fallback_reason,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class HandlerContext:
    request_id: str
    job_id: str
    handler_code: str
    worker_type: HandlerWorkerType
    runtime_table: HandlerRuntimeTable
    payload: dict[str, Any] = field(default_factory=dict)
    workflow_code: str = ""
    stage_code: str = ""
    job_code: str = ""
    item_code: str = ""
    business_key: str = ""
    dedupe_key: str = ""
    resource_code: str = ""
    worker_id: str = ""
    attempt_count: int = 0
    max_attempts: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "job_id": self.job_id,
            "handler_code": self.handler_code,
            "worker_type": self.worker_type,
            "runtime_table": self.runtime_table,
            "payload": dict(self.payload),
            "workflow_code": self.workflow_code,
            "stage_code": self.stage_code,
            "job_code": self.job_code,
            "item_code": self.item_code,
            "business_key": self.business_key,
            "dedupe_key": self.dedupe_key,
            "resource_code": self.resource_code,
            "worker_id": self.worker_id,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class HandlerContract:
    handler_code: str
    worker_type: HandlerWorkerType
    runtime_table: HandlerRuntimeTable
    purpose: str
    payload_schema: dict[str, Any] = field(default_factory=dict)
    result_schema: dict[str, Any] = field(default_factory=dict)
    error_schema: dict[str, Any] = field(default_factory=dict)
    retry_policy: dict[str, Any] = field(default_factory=dict)
    timeout_policy: dict[str, Any] = field(default_factory=dict)
    idempotency_policy: dict[str, Any] = field(default_factory=dict)
    side_effects: tuple[str, ...] = field(default_factory=tuple)
    progress_policy: dict[str, Any] = field(default_factory=dict)
    reconciler_contract: dict[str, Any] = field(default_factory=dict)
    contract_reference: str = ""

    def validate_context(self, context: HandlerContext) -> None:
        if context.handler_code != self.handler_code:
            raise ValueError(
                f"handler context code mismatch: expected {self.handler_code!r}, "
                f"got {context.handler_code!r}"
            )
        if context.worker_type != self.worker_type:
            raise ValueError(
                f"handler worker mismatch for {self.handler_code!r}: "
                f"expected {self.worker_type!r}, got {context.worker_type!r}"
            )
        if context.runtime_table != self.runtime_table:
            raise ValueError(
                f"handler runtime table mismatch for {self.handler_code!r}: "
                f"expected {self.runtime_table!r}, got {context.runtime_table!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "handler_code": self.handler_code,
            "worker_type": self.worker_type,
            "runtime_table": self.runtime_table,
            "purpose": self.purpose,
            "payload_schema": dict(self.payload_schema),
            "result_schema": dict(self.result_schema),
            "error_schema": dict(self.error_schema),
            "retry_policy": dict(self.retry_policy),
            "timeout_policy": dict(self.timeout_policy),
            "idempotency_policy": dict(self.idempotency_policy),
            "side_effects": list(self.side_effects),
            "progress_policy": dict(self.progress_policy),
            "reconciler_contract": dict(self.reconciler_contract),
            "contract_reference": self.contract_reference,
        }


@dataclass(frozen=True)
class HandlerResult:
    status: HandlerStatus
    handler_code: str
    request_id: str
    job_id: str
    summary: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    next_action: HandlerNextAction = field(default_factory=HandlerNextAction)
    error: HandlerError | None = None
    contract_revision: str = "phase1"

    @classmethod
    def success(
        cls,
        context: HandlerContext,
        *,
        summary: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        warnings: tuple[str, ...] | list[str] = (),
        next_action: HandlerNextAction | None = None,
    ) -> HandlerResult:
        return cls(
            status="success",
            handler_code=context.handler_code,
            request_id=context.request_id,
            job_id=context.job_id,
            summary=summary or {},
            result=result or {},
            warnings=tuple(warnings),
            next_action=next_action or HandlerNextAction(),
        )

    @classmethod
    def skipped(
        cls,
        context: HandlerContext,
        *,
        summary: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        warnings: tuple[str, ...] | list[str] = (),
    ) -> HandlerResult:
        return cls(
            status="skipped",
            handler_code=context.handler_code,
            request_id=context.request_id,
            job_id=context.job_id,
            summary=summary or {},
            result=result or {},
            warnings=tuple(warnings),
        )

    @classmethod
    def partial_success(
        cls,
        context: HandlerContext,
        *,
        summary: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        warnings: tuple[str, ...] | list[str] = (),
        next_action: HandlerNextAction | None = None,
    ) -> HandlerResult:
        return cls(
            status="partial_success",
            handler_code=context.handler_code,
            request_id=context.request_id,
            job_id=context.job_id,
            summary=summary or {},
            result=result or {},
            warnings=tuple(warnings),
            next_action=next_action or HandlerNextAction(),
        )

    @classmethod
    def failed(
        cls,
        context: HandlerContext,
        *,
        error: HandlerError,
        summary: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        warnings: tuple[str, ...] | list[str] = (),
    ) -> HandlerResult:
        return cls(
            status="failed",
            handler_code=context.handler_code,
            request_id=context.request_id,
            job_id=context.job_id,
            summary=summary or {},
            result=result or {},
            warnings=tuple(warnings),
            error=error,
        )

    @classmethod
    def fallback_required(
        cls,
        context: HandlerContext,
        *,
        error: HandlerError,
        summary: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        warnings: tuple[str, ...] | list[str] = (),
        next_action: HandlerNextAction | None = None,
    ) -> HandlerResult:
        return cls(
            status="fallback_required",
            handler_code=context.handler_code,
            request_id=context.request_id,
            job_id=context.job_id,
            summary=summary or {},
            result=result or {},
            warnings=tuple(warnings),
            next_action=next_action or HandlerNextAction(),
            error=error,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "handler_code": self.handler_code,
            "request_id": self.request_id,
            "job_id": self.job_id,
            "summary": dict(self.summary),
            "result": dict(self.result),
            "warnings": list(self.warnings),
            "next_action": self.next_action.to_dict(),
            "contract_revision": self.contract_revision,
        }
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        return payload


class HandlerCallable(Protocol):
    def __call__(self, context: HandlerContext) -> HandlerResult:
        ...
