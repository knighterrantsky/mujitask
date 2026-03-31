from __future__ import annotations

import argparse
import json
import traceback
import uuid
from pathlib import Path
from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult
from automation_framework.runtime import RunRegistry, WorkflowExecutor

from automation_business_scaffold.registry import build_task_registry

DEFAULT_CLI_RUN_DIR = Path("runtime/cli_runs")


def list_registered_tasks() -> list[dict[str, str]]:
    registry = build_task_registry()
    tasks: list[dict[str, str]] = []
    for task_name in registry.names():
        task = registry.get(task_name)
        if task is None:
            continue
        tasks.append(
            {
                "name": task.name,
                "description": str(getattr(task, "description", "") or ""),
            }
        )
    return tasks


def run_registered_task(
    task_name: str,
    params: dict[str, Any],
    *,
    run_dir: str | Path = DEFAULT_CLI_RUN_DIR,
    run_id: str | None = None,
) -> dict[str, Any]:
    registry = build_task_registry()
    task = registry.get(task_name)
    if task is None:
        available = ", ".join(registry.names())
        raise ValueError(f"Unknown task '{task_name}'. Available tasks: {available}")

    resolved_run_dir = Path(run_dir)
    run_registry = RunRegistry(str(resolved_run_dir))
    record = run_registry.create(
        run_id=run_id or uuid.uuid4().hex,
        task_name=task_name,
        params=params,
    )
    artifacts_dir = resolved_run_dir.parent / "artifacts" / record.run_id

    run_registry.update_status(record.run_id, "running", message=f"Task {task_name} started.")

    try:
        if isinstance(task, BaseWorkflowTask):
            result = WorkflowExecutor(run_registry).execute(
                run_id=record.run_id,
                task=task,
                params=params,
            )
        else:
            result = task.run(params)
        if not isinstance(result, FrameworkResult):
            raise TypeError(f"Task {task_name} must return FrameworkResult.")
        run_registry.update_status(
            record.run_id,
            "success",
            result=result.model_dump(mode="json"),
            message=f"Task {task_name} finished.",
        )
        status = "success"
        payload: dict[str, Any] = {
            "result": result.model_dump(mode="json"),
        }
    except Exception as exc:
        details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        run_registry.update_status(
            record.run_id,
            "failed",
            error=details,
            message=f"Task {task_name} failed.",
        )
        status = "failed"
        payload = {
            "error": details,
        }

    return {
        "run_id": record.run_id,
        "task_name": task_name,
        "status": status,
        "params": params,
        "run_file": str((resolved_run_dir / f"{record.run_id}.json").resolve()),
        "steps_file": str((resolved_run_dir / "steps" / f"{record.run_id}.json").resolve()),
        "signals_file": str((resolved_run_dir / "signals" / f"{record.run_id}.json").resolve()),
        "artifacts_dir": str(artifacts_dir.resolve()),
        **payload,
    }


def _load_json_object(source: str, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(source)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _parse_param_value(raw_value: str) -> Any:
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return raw_value


def _parse_param_items(items: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Invalid --param value '{item}'. Expected KEY=VALUE.")
        key, raw_value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid --param value '{item}'. KEY cannot be empty.")
        params[key] = _parse_param_value(raw_value)
    return params


def _build_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}

    if args.params_file:
        params_path = Path(args.params_file)
        params.update(_load_json_object(params_path.read_text(encoding="utf-8"), label="params file"))

    if args.params_json:
        params.update(_load_json_object(args.params_json, label="--params-json"))

    params.update(_parse_param_items(args.param or []))

    if args.product_url:
        params["product_url"] = args.product_url
    if args.run_mode:
        params["run_mode"] = args.run_mode
    if args.trace_id:
        params["trace_id"] = args.trace_id
    if args.field_mapping_json:
        params["field_mapping"] = _load_json_object(
            args.field_mapping_json,
            label="--field-mapping-json",
        )

    return params


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="automation-business-scaffold-run",
        description="Run scaffold tasks without starting the HTTP agent.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-tasks", help="List all registered tasks.")

    run_parser = subparsers.add_parser("run", help="Execute a registered task.")
    run_parser.add_argument("--task", required=True, help="Registered task name.")
    run_parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Repeatable task param. VALUE may be a JSON literal.",
    )
    run_parser.add_argument(
        "--params-json",
        help="Task params as a JSON object string.",
    )
    run_parser.add_argument(
        "--params-file",
        help="Path to a JSON file containing task params.",
    )
    run_parser.add_argument(
        "--product-url",
        help="Shortcut for setting product_url.",
    )
    run_parser.add_argument(
        "--run-mode",
        help="Shortcut for setting run_mode.",
    )
    run_parser.add_argument(
        "--trace-id",
        help="Shortcut for setting trace_id.",
    )
    run_parser.add_argument(
        "--field-mapping-json",
        help="Shortcut JSON object for field_mapping.",
    )
    run_parser.add_argument(
        "--run-dir",
        default=str(DEFAULT_CLI_RUN_DIR),
        help="Run registry directory. Defaults to runtime/cli_runs.",
    )
    run_parser.add_argument(
        "--run-id",
        help="Optional explicit run_id.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "list-tasks":
        print(json.dumps({"tasks": list_registered_tasks()}, ensure_ascii=False, indent=2))
        return 0

    try:
        params = _build_params(args)
        payload = run_registered_task(
            task_name=args.task,
            params=params,
            run_dir=args.run_dir,
            run_id=args.run_id,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
