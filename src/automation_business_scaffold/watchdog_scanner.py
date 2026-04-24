from __future__ import annotations

import argparse
import contextlib
import json
import sys
from typing import Any

from automation_business_scaffold.business.flows.watchdog_scanner import (
    execute_watchdog_scan_once,
    run_watchdog_scanner,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="automation-business-scaffold-watchdog",
        description="Run the watchdog scanner against Runtime DB records.",
    )
    parser.add_argument("--once", action="store_true", help="Run a single watchdog scan.")
    parser.add_argument("--db-url")
    parser.add_argument("--poll-interval-seconds", type=float)
    parser.add_argument("--limit-per-rule", type=int)
    parser.add_argument("--dry-run", action="store_true", help="Decide actions without applying them.")
    parser.add_argument("--stop-when-idle", action="store_true")
    parser.add_argument("--max-idle-cycles", type=int, default=1)
    parser.add_argument("--max-iterations", type=int, default=0)
    return parser


def _build_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {"apply_actions": not args.dry_run}
    if args.db_url:
        params["execution_control_db_url"] = args.db_url
    if args.poll_interval_seconds is not None:
        params["execution_control_poll_interval_seconds"] = args.poll_interval_seconds
    if args.limit_per_rule is not None:
        params["limit_per_rule"] = max(args.limit_per_rule, 1)
    if not args.once:
        params["execution_control_stop_when_idle"] = args.stop_when_idle
        params["execution_control_max_idle_cycles"] = max(args.max_idle_cycles, 1)
        params["max_iterations"] = max(args.max_iterations, 0)
    return params


def _exit_code(payload: dict[str, Any]) -> int:
    action_count = int(payload.get("action_count", 0) or 0)
    failed_count = int(payload.get("counts_by_action", {}).get("fail", 0) or 0)
    if failed_count > 0:
        return 1
    if payload.get("status") == "failed":
        return 1
    if action_count == 0:
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    params = _build_params(args)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            payload = execute_watchdog_scan_once(params) if args.once else run_watchdog_scanner(params)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return _exit_code(payload)


if __name__ == "__main__":
    raise SystemExit(main())

