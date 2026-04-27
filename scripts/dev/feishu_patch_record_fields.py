#!/usr/bin/env python3
"""Patch Feishu Bitable record fields during development testing.

The script updates record field values through the same Feishu client used by
the application. It is dry-run by default; pass --apply to write changes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from automation_business_scaffold.infrastructure.feishu.api import (  # noqa: E402
    FeishuBitableClient,
    parse_table_url,
)


DEFAULT_INFLUENCER_DISPLAY_FIELDS = ("粉丝数", "带货视频 GMV", "带货直播 GMV")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patch fields on a single Feishu Bitable record for development testing.",
    )
    parser.add_argument("--table-url", required=True, help="Feishu Bitable table URL.")
    parser.add_argument("--record-id", default="", help="Target Bitable record id.")
    parser.add_argument(
        "--access-token",
        default="",
        help="Feishu access token. Prefer --access-token-env for local use.",
    )
    parser.add_argument(
        "--access-token-env",
        default="MUJITASK_FEISHU_ACCESS_TOKEN",
        help="Environment variable containing the Feishu access token.",
    )
    parser.add_argument(
        "--fields-json",
        default="",
        help='JSON object of fields to update, for example: {"Field":"value"}.',
    )
    parser.add_argument(
        "--fields-file",
        default="",
        help="Path to a JSON object file containing fields to update.",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="FIELD=VALUE",
        help="Set one field. VALUE is parsed as JSON when possible, otherwise as text.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually update the record. Without this flag, only prints a dry-run preview.",
    )
    parser.add_argument(
        "--print-before",
        action="store_true",
        help="Print existing field values before applying the patch.",
    )
    parser.add_argument(
        "--format-influencer-display-fields",
        action="store_true",
        help="Batch-format influencer count/GMV fields as W display values.",
    )
    parser.add_argument(
        "--format-field",
        action="append",
        default=[],
        help="Field to format in batch mode. Defaults to influencer follower/video GMV/live GMV.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Page size when scanning records in batch mode.",
    )
    args = parser.parse_args()

    access_token = _resolve_access_token(args.access_token, args.access_token_env)
    table_meta = parse_table_url(args.table_url)
    client = FeishuBitableClient(access_token)

    if args.format_influencer_display_fields:
        return _format_influencer_display_fields(
            client=client,
            table_meta=table_meta,
            field_names=tuple(args.format_field or DEFAULT_INFLUENCER_DISPLAY_FIELDS),
            page_size=args.page_size,
            apply_changes=args.apply,
        )

    if not str(args.record_id or "").strip():
        raise ValueError("--record-id is required unless --format-influencer-display-fields is used.")
    fields = _build_fields_patch(
        fields_json=args.fields_json,
        fields_file=args.fields_file,
        set_values=args.set,
    )
    if not fields:
        raise ValueError("No fields to update. Use --fields-json, --fields-file, or --set.")

    if args.print_before or not args.apply:
        before = client.get_record(table_meta["app_token"], table_meta["table_id"], args.record_id)
        print(json.dumps({"before": _extract_record_fields(before)}, ensure_ascii=False, indent=2))

    preview = {
        "table": {
            "app_token": table_meta["app_token"],
            "table_id": table_meta["table_id"],
            "view_id": table_meta.get("view_id", ""),
        },
        "record_id": args.record_id,
        "fields": fields,
        "mode": "apply" if args.apply else "dry_run",
    }
    print(json.dumps(preview, ensure_ascii=False, indent=2))

    if not args.apply:
        return 0

    response = client.update_record(
        table_meta["app_token"],
        table_meta["table_id"],
        args.record_id,
        fields,
    )
    print(json.dumps({"updated": _extract_record_fields(response)}, ensure_ascii=False, indent=2))
    return 0


def _format_influencer_display_fields(
    *,
    client: FeishuBitableClient,
    table_meta: Mapping[str, str],
    field_names: Sequence[str],
    page_size: int,
    apply_changes: bool,
) -> int:
    records = client.list_all_records(
        table_meta["app_token"],
        table_meta["table_id"],
        page_size=max(page_size, 1),
    )
    planned_updates: list[dict[str, Any]] = []
    for record in records:
        record_id = str(record.get("record_id", "") or "").strip()
        raw_fields = record.get("fields")
        if not record_id or not isinstance(raw_fields, Mapping):
            continue

        patch: dict[str, str] = {}
        before: dict[str, str] = {}
        for field_name in field_names:
            current_value = _extract_scalar_text(raw_fields.get(field_name))
            formatted_value = _format_w_display_value(current_value)
            if not formatted_value or formatted_value == current_value:
                continue
            before[field_name] = current_value
            patch[field_name] = formatted_value

        if patch:
            planned_updates.append(
                {
                    "record_id": record_id,
                    "before": before,
                    "fields": patch,
                }
            )

    print(
        json.dumps(
            {
                "mode": "apply" if apply_changes else "dry_run",
                "total_records": len(records),
                "update_count": len(planned_updates),
                "fields": list(field_names),
                "updates": planned_updates,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    if not apply_changes:
        return 0

    for item in planned_updates:
        client.update_record(
            table_meta["app_token"],
            table_meta["table_id"],
            str(item["record_id"]),
            dict(item["fields"]),
        )
    print(json.dumps({"applied_update_count": len(planned_updates)}, ensure_ascii=False, indent=2))
    return 0


def _resolve_access_token(access_token: str, access_token_env: str) -> str:
    direct_token = str(access_token or "").strip()
    if direct_token:
        return direct_token
    env_name = str(access_token_env or "").strip()
    if env_name:
        env_value = str(os.getenv(env_name, "") or "").strip()
        if env_value:
            return env_value
    raise ValueError("Feishu access token is required. Set --access-token or --access-token-env.")


def _build_fields_patch(
    *,
    fields_json: str,
    fields_file: str,
    set_values: list[str],
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    fields.update(_load_fields_json(fields_json, source="--fields-json"))
    if fields_file:
        path = Path(fields_file).expanduser()
        fields.update(_load_fields_json(path.read_text(encoding="utf-8"), source=str(path)))
    for item in set_values:
        field_name, value = _parse_set_value(item)
        fields[field_name] = value
    return fields


def _load_fields_json(raw_value: str, *, source: str) -> dict[str, Any]:
    text = str(raw_value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} must be a JSON object: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{source} must be a JSON object.")
    return {str(key): value for key, value in payload.items()}


def _parse_set_value(raw_value: str) -> tuple[str, Any]:
    if "=" not in raw_value:
        raise ValueError("--set must use FIELD=VALUE format.")
    field_name, value_text = raw_value.split("=", 1)
    field_name = field_name.strip()
    if not field_name:
        raise ValueError("--set field name cannot be empty.")
    return field_name, _parse_jsonish_value(value_text)


def _parse_jsonish_value(value_text: str) -> Any:
    text = value_text.strip()
    if text == "":
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value_text


def _extract_scalar_text(value: Any) -> str:
    if value in (None, "", [], (), {}):
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return _format_trimmed_decimal(float(value), max_digits=2)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("text", "value", "name", "link"):
            text = _extract_scalar_text(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parts = [_extract_scalar_text(item) for item in value]
        return "".join(part for part in parts if part).strip()
    return str(value).strip()


def _format_w_display_value(value: Any) -> str:
    text = _extract_scalar_text(value)
    if not text:
        return ""
    number = _coerce_metric_number(text)
    if number is None:
        return text
    if abs(number) >= 10_000:
        return f"{_format_trimmed_decimal(number / 10_000, max_digits=2)}W"
    return _format_trimmed_decimal(number, max_digits=2)


def _coerce_metric_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace(",", "").replace(" ", "")
    text = text.strip("。")
    if not text:
        return None

    multiplier = 1.0
    match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*([万亿wWkKmMbB]?)", text)
    if not match:
        return None

    unit = match.group(2).lower()
    if unit in {"万", "w"}:
        multiplier = 10_000.0
    elif unit == "亿":
        multiplier = 100_000_000.0
    elif unit == "k":
        multiplier = 1_000.0
    elif unit == "m":
        multiplier = 1_000_000.0
    elif unit == "b":
        multiplier = 1_000_000_000.0

    try:
        return float(match.group(1)) * multiplier
    except ValueError:
        return None


def _format_trimmed_decimal(value: float, *, max_digits: int) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.{max_digits}f}".rstrip("0").rstrip(".")


def _extract_record_fields(response: Mapping[str, Any]) -> dict[str, Any]:
    data = response.get("data")
    if isinstance(data, Mapping):
        record = data.get("record")
        if isinstance(record, Mapping):
            fields = record.get("fields")
            if isinstance(fields, Mapping):
                return dict(fields)
    return {}


if __name__ == "__main__":
    raise SystemExit(main())
