#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable


TASK_CODES = {
    "refresh_amazon_product_row_by_asin",
    "refresh_current_amazon_product_table",
}


def _load_submitter(
    install_dir: Path,
    task_code: str,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    src_dir = install_dir / "src"
    if not src_dir.exists():
        raise ValueError(f"Cannot find project source directory at {src_dir}.")
    src_path = str(src_dir)
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    from automation_business_scaffold.control_plane.executor.runner import (  # pylint: disable=import-outside-toplevel
        run_refresh_amazon_product_row_by_asin_request,
        run_refresh_current_amazon_product_table_request,
    )

    if task_code == "refresh_current_amazon_product_table":
        return run_refresh_current_amazon_product_table_request
    return run_refresh_amazon_product_row_by_asin_request


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Submit an Amazon Runtime task.")
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--task-code", required=True, choices=sorted(TASK_CODES))
    parser.add_argument("--params-json", required=True)
    parser.add_argument("--result-file", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    install_dir = Path(args.install_dir).expanduser().resolve()
    result_file = Path(args.result_file).expanduser().resolve()
    params = json.loads(args.params_json)
    if not isinstance(params, dict):
        raise ValueError("--params-json must decode to a JSON object.")
    if str(params.get("control_action") or "submit").strip() != "submit":
        raise ValueError("Amazon skill submit only supports control_action=submit.")

    payload = _load_submitter(install_dir, args.task_code)(params)
    if not isinstance(payload, dict):
        raise TypeError(f"{args.task_code} submitter must return a JSON object.")
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
