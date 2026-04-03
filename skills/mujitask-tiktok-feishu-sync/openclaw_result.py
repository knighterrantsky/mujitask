#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _absolute_path(value: str | None) -> str:
    if not value:
        return ""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()
    return str(path)


def _load_json_file(value: str | None) -> Any:
    if not value:
        return {}
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_summary_text(summary: dict[str, Any]) -> str:
    if not isinstance(summary, dict):
        return ""
    counts = summary.get("counts", {})
    if not isinstance(counts, dict) or not counts:
        total = summary.get("total")
        return f"total={total}" if total is not None else ""
    parts = [f"{key}={counts[key]}" for key in sorted(counts)]
    total = summary.get("total")
    if total is not None:
        parts.append(f"total={total}")
    return ", ".join(parts)


def build_run_summary(args: argparse.Namespace) -> dict[str, Any]:
    payload = _load_json_file(args.run_file)
    result_payload: dict[str, Any] = {
        "status": args.status or "unknown",
        "task_name": args.fallback_task or "",
        "run_id": args.run_id or "",
        "run_file": _absolute_path(args.run_file),
        "steps_file": _absolute_path(args.steps_file),
        "signals_file": _absolute_path(args.signals_file),
        "stdout_file": _absolute_path(args.stdout_file),
        "message": "",
        "summary": {"total": 0, "counts": {}},
        "summary_text": "",
        "failed_item_count": 0,
        "error": args.error_message or "",
    }

    if isinstance(payload, dict) and payload:
        result = payload.get("result", {})
        result_data = result.get("data", {}) if isinstance(result, dict) else {}
        step_outputs = result_data.get("step_outputs", {}) if isinstance(result_data, dict) else {}
        emit_summary = step_outputs.get("emit_summary", {}) if isinstance(step_outputs, dict) else {}
        summary = result_data.get("summary", {}) if isinstance(result_data, dict) else {}
        failed_items = result_data.get("failed_items", []) if isinstance(result_data, dict) else []

        if isinstance(emit_summary, dict):
            summary = emit_summary.get("summary", summary)
            failed_items = emit_summary.get("failed_items", failed_items)

        error = payload.get("error")

        result_payload.update(
            {
                "status": str(payload.get("status", "") or result_payload["status"]),
                "task_name": str(payload.get("task_name", "") or result_payload["task_name"]),
                "run_id": str(payload.get("run_id", "") or result_payload["run_id"]),
                "message": str(
                    (
                        result.get("message", "")
                        if isinstance(result, dict)
                        else ""
                    )
                    or payload.get("message", "")
                    or ""
                ),
                "summary": summary if isinstance(summary, dict) else result_payload["summary"],
                "failed_item_count": len(failed_items) if isinstance(failed_items, list) else 0,
            }
        )
        if isinstance(error, str) and error.strip():
            result_payload["error"] = error.strip()

    if args.error_message and not str(result_payload.get("error", "")).strip():
        result_payload["error"] = args.error_message

    result_payload["summary_text"] = _build_summary_text(result_payload.get("summary", {}))
    return result_payload


def build_combined_summary(args: argparse.Namespace) -> dict[str, Any]:
    cleanup = _load_json_file(args.cleanup_result_file)
    batch = _load_json_file(args.batch_result_file)

    cleanup_payload = cleanup if isinstance(cleanup, dict) and cleanup else None
    batch_payload = batch if isinstance(batch, dict) and batch else None

    combined: dict[str, Any] = {
        "status": args.status or "unknown",
        "task_name": args.task_name,
        "cleanup": cleanup_payload,
        "batch": batch_payload,
        "message": args.message or "",
        "error": args.error_message or "",
    }

    primary = batch_payload or cleanup_payload or {}
    if isinstance(primary, dict):
        if primary.get("run_id"):
            combined["run_id"] = primary["run_id"]
        if primary.get("summary"):
            combined["summary"] = primary["summary"]
        if primary.get("summary_text"):
            combined["summary_text"] = primary["summary_text"]

    failed_item_count = 0
    if isinstance(batch_payload, dict):
        failed_item_count = int(batch_payload.get("failed_item_count", 0) or 0)
    combined["failed_item_count"] = failed_item_count

    if not combined["message"]:
        if combined["status"] == "success":
            combined["message"] = "TikTok Feishu sync completed."
        elif args.error_message:
            combined["message"] = args.error_message
        elif batch_payload and batch_payload.get("message"):
            combined["message"] = str(batch_payload["message"])
        elif cleanup_payload and cleanup_payload.get("message"):
            combined["message"] = str(cleanup_payload["message"])

    if not str(combined.get("error", "")).strip():
        if isinstance(batch_payload, dict) and str(batch_payload.get("error", "")).strip():
            combined["error"] = str(batch_payload["error"]).strip()
        elif isinstance(cleanup_payload, dict) and str(cleanup_payload.get("error", "")).strip():
            combined["error"] = str(cleanup_payload["error"]).strip()

    return combined


def main() -> int:
    parser = argparse.ArgumentParser(description="Build compact OpenClaw result payloads.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run-summary", help="Summarize one runtime run.")
    run_parser.add_argument("--run-file", required=True)
    run_parser.add_argument("--steps-file")
    run_parser.add_argument("--signals-file")
    run_parser.add_argument("--stdout-file")
    run_parser.add_argument("--run-id")
    run_parser.add_argument("--fallback-task")
    run_parser.add_argument("--status")
    run_parser.add_argument("--error-message")

    combine_parser = subparsers.add_parser("combine", help="Combine cleanup and batch results.")
    combine_parser.add_argument("--cleanup-result-file")
    combine_parser.add_argument("--batch-result-file")
    combine_parser.add_argument("--task-name", default="feishu_tiktok_sync")
    combine_parser.add_argument("--status")
    combine_parser.add_argument("--message")
    combine_parser.add_argument("--error-message")

    args = parser.parse_args()

    if args.command == "run-summary":
        payload = build_run_summary(args)
    else:
        payload = build_combined_summary(args)

    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
