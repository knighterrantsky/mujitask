from __future__ import annotations

import json
from typing import Any

import requests

from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    coerce_bool,
    coerce_mapping,
    coerce_str,
    failed_result,
    first_non_empty,
    success_result,
)
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.contracts.handler.allowlist import OUTBOX_HANDLER_CONTRACTS

RETRYABLE_WEBHOOK_STATUS_CODES = {408, 409, 425, 429}


HANDLER_CODE = "outbox_dispatch"
CONTRACT = OUTBOX_HANDLER_CONTRACTS[HANDLER_CODE]


def outbox_dispatch_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    channel_code = first_non_empty(
        payload.get("channel_code"),
        context.metadata.get("channel_code"),
        "stdout",
    )
    reply_target = first_non_empty(payload.get("reply_target"), context.metadata.get("reply_target"))
    message = _render_message(payload)
    dry_run = coerce_bool(payload.get("dry_run"), default=True)
    _report_progress(
        context,
        "dispatching",
        message=f"Dispatching outbox message through {channel_code}.",
        details={"channel_code": channel_code, "reply_target": reply_target},
    )

    if channel_code in {"noop", "disabled"}:
        _report_progress(
            context,
            "dispatch_skipped",
            message=f"Outbox channel {channel_code} does not emit notifications.",
            details={"channel_code": channel_code},
        )
        return success_result(
            context,
            summary={"channel_code": channel_code, "delivery_state": "skipped", "message_length": len(message)},
            result={
                "channel_code": channel_code,
                "reply_target": reply_target,
                "message": message,
                "delivery_state": "skipped",
            },
        )

    if channel_code in {"stdout", "console"}:
        if not dry_run and coerce_bool(payload.get("emit_to_stdout"), default=True):
            print(message)
        delivery_state = "sent" if not dry_run else "simulated"
        _report_progress(
            context,
            f"dispatch_{delivery_state}",
            message=f"Outbox message {delivery_state} through {channel_code}.",
            details={"channel_code": channel_code, "delivery_state": delivery_state},
        )
        return success_result(
            context,
            summary={"channel_code": channel_code, "delivery_state": delivery_state, "message_length": len(message)},
            result={
                "channel_code": channel_code,
                "reply_target": reply_target,
                "message": message,
                "delivery_state": delivery_state,
            },
        )

    if channel_code == "webhook":
        webhook_url = coerce_str(payload.get("webhook_url"))
        if not webhook_url:
            _report_progress(
                context,
                "dispatch_failed",
                message="Webhook dispatch is missing its URL.",
                details={"channel_code": channel_code, "retryable": False},
            )
            error = build_error(
                error_type="dispatch_failure",
                error_code="outbox_webhook_missing_url",
                message="Webhook dispatch requires payload.webhook_url.",
                retryable=False,
                details={"channel_code": channel_code},
            )
            return failed_result(context, error=error, summary={"channel_code": channel_code})
        if dry_run:
            _report_progress(
                context,
                "dispatch_simulated",
                message="Webhook dispatch simulated in dry-run mode.",
                details={"channel_code": channel_code, "webhook_url": webhook_url},
            )
            return success_result(
                context,
                summary={"channel_code": channel_code, "delivery_state": "simulated", "message_length": len(message)},
                result={
                    "channel_code": channel_code,
                    "reply_target": reply_target,
                    "message": message,
                    "delivery_state": "simulated",
                    "webhook_url": webhook_url,
                },
            )
        try:
            response = requests.post(
                webhook_url,
                json={"message": message, "payload": coerce_mapping(payload.get("body")) or payload},
                timeout=float(payload.get("timeout_seconds", 10.0) or 10.0),
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            retryable = _is_retryable_webhook_error(exc)
            status_code = _request_status_code(exc)
            _report_progress(
                context,
                "dispatch_retryable_failure" if retryable else "dispatch_terminal_failure",
                message=str(exc),
                details=_compact_details(
                    {
                        "channel_code": channel_code,
                        "webhook_url": webhook_url,
                        "status_code": status_code,
                        "retryable": retryable,
                    }
                ),
            )
            error = build_error(
                error_type="dispatch_failure",
                error_code="outbox_webhook_request_failed",
                message=str(exc),
                retryable=retryable,
                details=_compact_details(
                    {"channel_code": channel_code, "webhook_url": webhook_url, "status_code": status_code}
                ),
            )
            return failed_result(context, error=error, summary={"channel_code": channel_code})
        _report_progress(
            context,
            "dispatch_sent",
            message="Webhook dispatch completed.",
            details={"channel_code": channel_code, "webhook_url": webhook_url, "status_code": response.status_code},
        )
        return success_result(
            context,
            summary={"channel_code": channel_code, "delivery_state": "sent", "message_length": len(message)},
            result={
                "channel_code": channel_code,
                "reply_target": reply_target,
                "message": message,
                "delivery_state": "sent",
                "webhook_url": webhook_url,
                "status_code": response.status_code,
            },
        )

    if dry_run:
        _report_progress(
            context,
            "dispatch_simulated",
            message=f"Unsupported outbox channel {channel_code} simulated in dry-run mode.",
            details={"channel_code": channel_code},
        )
        return success_result(
            context,
            summary={"channel_code": channel_code, "delivery_state": "simulated", "message_length": len(message)},
            result={
                "channel_code": channel_code,
                "reply_target": reply_target,
                "message": message,
                "delivery_state": "simulated",
            },
            warnings=(f"Unsupported outbox channel '{channel_code}' simulated in dry-run mode.",),
        )

    _report_progress(
        context,
        "dispatch_failed",
        message=f"Unsupported outbox channel {channel_code}.",
        details={"channel_code": channel_code, "retryable": False},
    )
    error = build_error(
        error_type="dispatch_failure",
        error_code="outbox_channel_unsupported",
        message=f"Unsupported outbox channel '{channel_code}'.",
        retryable=False,
        details={"channel_code": channel_code},
    )
    return failed_result(context, error=error, summary={"channel_code": channel_code})


def _render_message(payload: dict[str, Any]) -> str:
    for key in ("message", "message_text", "text"):
        text = coerce_str(payload.get(key))
        if text:
            return text
    summary = coerce_mapping(payload.get("summary"))
    result = coerce_mapping(payload.get("result"))
    if summary or result:
        return json.dumps(
            {"summary": summary, "result": result},
            ensure_ascii=False,
            sort_keys=True,
        )
    event_type = first_non_empty(payload.get("event_type"), "task_notification")
    ref_type = first_non_empty(payload.get("ref_type"), "task_request")
    ref_id = first_non_empty(payload.get("ref_id"), payload.get("request_id"))
    return f"{event_type} {ref_type} {ref_id}".strip()


def _report_progress(
    context: HandlerContext,
    progress_stage: str,
    *,
    message: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    progress_callback = context.metadata.get("progress_callback")
    if callable(progress_callback):
        progress_callback(progress_stage, message=message, details=details or {})


def _request_status_code(exc: requests.RequestException) -> int | None:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        return None
    try:
        return int(status_code)
    except (TypeError, ValueError):
        return None


def _is_retryable_webhook_error(exc: requests.RequestException) -> bool:
    status_code = _request_status_code(exc)
    if status_code is None:
        return True
    if status_code >= 500:
        return True
    return status_code in RETRYABLE_WEBHOOK_STATUS_CODES


def _compact_details(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in (None, "")}


__all__ = ["CONTRACT", "HANDLER_CODE", "outbox_dispatch_handler"]
