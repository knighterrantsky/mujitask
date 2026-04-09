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


def _pick_single_step_output(step_outputs: Any) -> dict[str, Any]:
    if not isinstance(step_outputs, dict):
        return {}
    if len(step_outputs) != 1:
        return {}
    only_value = next(iter(step_outputs.values()), {})
    return only_value if isinstance(only_value, dict) else {}


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
        single_step_output = _pick_single_step_output(step_outputs)
        summary = result_data.get("summary", {}) if isinstance(result_data, dict) else {}
        failed_items = result_data.get("failed_items", []) if isinstance(result_data, dict) else []

        if isinstance(emit_summary, dict) and emit_summary:
            summary = emit_summary.get("summary", summary)
            failed_items = emit_summary.get("failed_items", failed_items)
        elif not summary and single_step_output:
            summary = single_step_output.get("summary", summary)
            failed_items = single_step_output.get("failed_items", failed_items)

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

    args = parser.parse_args()
    payload = build_run_summary(args)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
