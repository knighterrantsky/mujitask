from __future__ import annotations

import argparse
import contextlib
import json
import sys
from typing import Any

from automation_business_scaffold.business.flows.runtime_orchestrator import (
    execute_api_worker_once,
    run_api_worker_daemon,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="automation-business-scaffold-api-worker",
        description="Run the API/batch worker queue consumer.",
    )
    parser.add_argument("--once", action="store_true", help="Process at most one API worker cycle.")
    parser.add_argument("--db-url")
    parser.add_argument("--request-id")
    parser.add_argument("--worker-id")
    parser.add_argument("--lease-seconds", type=float)
    parser.add_argument("--heartbeat-interval-seconds", type=float)
    parser.add_argument("--poll-interval-seconds", type=float)
    parser.add_argument("--stop-when-idle", action="store_true")
    parser.add_argument("--max-idle-cycles", type=int, default=1)
    parser.add_argument("--max-iterations", type=int, default=0)
    return parser


def _build_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if args.db_url:
        params["execution_control_db_url"] = args.db_url
    if args.request_id:
        params["request_id"] = args.request_id
    if args.worker_id:
        params["execution_worker_id"] = args.worker_id
    if args.lease_seconds is not None:
        params["execution_lease_seconds"] = args.lease_seconds
    if args.heartbeat_interval_seconds is not None:
        params["execution_heartbeat_interval_seconds"] = args.heartbeat_interval_seconds
    if args.poll_interval_seconds is not None:
        params["execution_control_poll_interval_seconds"] = args.poll_interval_seconds
    if not args.once:
        params["execution_control_stop_when_idle"] = args.stop_when_idle
        params["execution_control_max_idle_cycles"] = max(args.max_idle_cycles, 1)
        params["execution_control_max_iterations"] = max(args.max_iterations, 0)
    return params


def _exit_code(payload: dict[str, Any]) -> int:
    if int(payload.get("failed_count", 0) or 0) > 0:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    params = _build_params(args)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            payload = execute_api_worker_once(params) if args.once else run_api_worker_daemon(params)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return _exit_code(payload)


if __name__ == "__main__":
    raise SystemExit(main())
