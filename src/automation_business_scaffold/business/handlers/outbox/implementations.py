from __future__ import annotations

import json
from typing import Any

import requests

from .._shared import (
    build_error,
    coerce_bool,
    coerce_mapping,
    coerce_str,
    failed_result,
    first_non_empty,
    success_result,
)
from ..contract import HandlerContext, HandlerResult


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

    if channel_code in {"noop", "disabled"}:
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
        return success_result(
            context,
            summary={"channel_code": channel_code, "delivery_state": "sent", "message_length": len(message)},
            result={
                "channel_code": channel_code,
                "reply_target": reply_target,
                "message": message,
                "delivery_state": "sent" if not dry_run else "simulated",
            },
        )

    if channel_code == "webhook":
        webhook_url = coerce_str(payload.get("webhook_url"))
        if not webhook_url:
            error = build_error(
                error_type="dispatch_failure",
                error_code="outbox_webhook_missing_url",
                message="Webhook dispatch requires payload.webhook_url.",
                retryable=False,
                details={"channel_code": channel_code},
            )
            return failed_result(context, error=error, summary={"channel_code": channel_code})
        if dry_run:
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
            error = build_error(
                error_type="dispatch_failure",
                error_code="outbox_webhook_request_failed",
                message=str(exc),
                retryable=True,
                details={"channel_code": channel_code, "webhook_url": webhook_url},
            )
            return failed_result(context, error=error, summary={"channel_code": channel_code})
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

    error = build_error(
        error_type="dispatch_failure",
        error_code="outbox_channel_unsupported",
        message=f"Unsupported outbox channel '{channel_code}'.",
        retryable=False,
        details={"channel_code": channel_code},
    )
    return failed_result(context, error=error, summary={"channel_code": channel_code})


def _render_message(payload: dict[str, Any]) -> str:
    for key in ("message", "text"):
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
