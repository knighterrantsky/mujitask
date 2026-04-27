#!/usr/bin/env python3
"""Backfill the first FastMoss contact into the current Feishu influencer table."""

from __future__ import annotations

import argparse
import random
import re
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from automation_business_scaffold.infrastructure.fastmoss.http_session import (
    FastMossHTTPError,
    FastMossHTTPSession,
)
from automation_business_scaffold.infrastructure.feishu.api import FeishuBitableClient, parse_table_url


DEFAULT_ENV_FILE = "skills/mujitask-tiktok-feishu-sync/skill.local.env"
DEFAULT_ID_FIELD_NAME = "达人ID"
DEFAULT_CONTACT_FIELD_NAME = "达人联系方式"
CONTACT_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
CONTACT_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
CONTACT_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")


def compose_feishu_table_url(base_url: str, table_id: str, view_id: str) -> str:
    parsed = urlparse(base_url.strip())
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["table"] = table_id.strip()
    query["view"] = view_id.strip()
    path = parsed.path.rstrip("/") or parsed.path
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, urlencode(query), parsed.fragment))


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def require_env(values: Mapping[str, str], key: str) -> str:
    value = str(values.get(key, "") or "").strip()
    if not value:
        raise ValueError(f"{key} is required in the env file.")
    return value


def text_field_value(value: Any) -> str:
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                parts.append(str(item.get("text") or item.get("name") or ""))
            else:
                parts.append(str(item or ""))
        return "".join(parts).strip()
    return str(value or "").strip()


def normalize_unique_id(value: Any) -> str:
    return str(value or "").strip().lstrip("@").lower()


def extract_author_rows(search_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    data = search_payload.get("data")
    if not isinstance(data, Mapping):
        return []
    for key in ("author_list", "list", "items"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, Mapping)]
    return []


def extract_contact_items(contact_payload: Any) -> list[dict[str, Any]]:
    if isinstance(contact_payload, Mapping):
        rows = contact_payload.get("list")
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, Mapping)]
        data = contact_payload.get("data")
        if isinstance(data, Mapping):
            rows = data.get("list")
            if isinstance(rows, list):
                return [dict(row) for row in rows if isinstance(row, Mapping)]
        if "has" in contact_payload or "name" in contact_payload:
            return [dict(contact_payload)]
    if isinstance(contact_payload, list):
        return [dict(row) for row in contact_payload if isinstance(row, Mapping)]
    return []


def is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def first_contact_text(contact_payload: Any) -> str:
    for item in extract_contact_items(contact_payload):
        if not is_truthy(item.get("has")):
            continue
        channel = str(item.get("name") or item.get("channel_name") or item.get("id") or "").strip()
        if not channel:
            continue
        value = contact_value_for_channel(item, channel_key=channel.lower())
        if value:
            return f"{channel}:{value}"
    return ""


def contact_value_for_channel(item: Mapping[str, Any], *, channel_key: str) -> str:
    if channel_key == "email":
        return first_email(first_non_empty(item.get("id"), item.get("channel_name"), item.get("link")))
    if channel_key == "bio":
        return contactable_text(
            first_non_empty(item.get("link"), item.get("channel_name"), item.get("value"), item.get("text"))
        )
    for key in ("link", "id", "channel_name", "value", "text"):
        value = str(item.get(key) or "").strip()
        if value and value.lower() != channel_key:
            return value
    return ""


def contactable_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    url_match = CONTACT_URL_RE.search(text)
    if url_match:
        return url_match.group(0).rstrip(".,;")
    email = first_email(text)
    if email:
        return email
    phone_match = CONTACT_PHONE_RE.search(text)
    if phone_match:
        return phone_match.group(0).strip()
    return ""


def first_email(value: Any) -> str:
    match = CONTACT_EMAIL_RE.search(str(value or ""))
    return match.group(0) if match else ""


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def resolve_author_uid(fastmoss: FastMossHTTPSession, influencer_id: str) -> tuple[str, str, int]:
    normalized_id = str(influencer_id or "").strip()
    if normalized_id.isdigit():
        return normalized_id, "numeric_uid", 1

    search_payload = fastmoss.search_author(normalized_id)
    rows = extract_author_rows(search_payload)
    normalized_unique_id = normalize_unique_id(normalized_id)
    for row in rows:
        if normalize_unique_id(row.get("unique_id")) != normalized_unique_id:
            continue
        uid = str(row.get("uid") or "").strip()
        if uid:
            return uid, "exact_unique_id", len(rows)

    if len(rows) == 1:
        uid = str(rows[0].get("uid") or "").strip()
        if uid:
            return uid, "single_search_result", len(rows)

    return "", "not_found" if not rows else "ambiguous", len(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--id-field-name", default=DEFAULT_ID_FIELD_NAME)
    parser.add_argument("--contact-field-name", default=DEFAULT_CONTACT_FIELD_NAME)
    parser.add_argument("--limit", type=int, default=0, help="Max missing rows to process; 0 means all.")
    parser.add_argument(
        "--only-influencer-id",
        action="append",
        default=[],
        help="Only process the given influencer ID. Can be passed more than once.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch contacts but do not update Feishu.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing contact values too.")
    parser.add_argument("--request-delay-min-seconds", type=float, default=0.5)
    parser.add_argument("--request-delay-max-seconds", type=float, default=1.2)
    parser.add_argument("--feishu-update-delay-seconds", type=float, default=0.15)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    env_values = load_env_file(Path(args.env_file))

    target_table_url = compose_feishu_table_url(
        require_env(env_values, "MUJITASK_FEISHU_BASE_URL"),
        require_env(env_values, "MUJITASK_FEISHU_TK_INFLUENCER_POOL_TABLE_ID"),
        require_env(env_values, "MUJITASK_FEISHU_TK_INFLUENCER_POOL_VIEW_ID"),
    )
    feishu_token = require_env(env_values, "MUJITASK_FEISHU_ACCESS_TOKEN")
    fastmoss_phone = require_env(env_values, "FASTMOSS_PHONE")
    fastmoss_password = require_env(env_values, "FASTMOSS_PASSWORD")

    table_meta = parse_table_url(target_table_url)
    feishu = FeishuBitableClient(feishu_token)
    records = feishu.list_all_records(
        table_meta["app_token"],
        table_meta["table_id"],
        page_size=100,
        view_id=table_meta.get("view_id") or None,
    )

    only_influencer_ids = {
        normalize_unique_id(value)
        for value in (args.only_influencer_id or [])
        if str(value or "").strip()
    }
    candidates: list[dict[str, Any]] = []
    existing_contact_count = 0
    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, Mapping):
            continue
        influencer_id = text_field_value(fields.get(args.id_field_name))
        contact = text_field_value(fields.get(args.contact_field_name))
        if contact:
            existing_contact_count += 1
        if not influencer_id:
            continue
        if only_influencer_ids and normalize_unique_id(influencer_id) not in only_influencer_ids:
            continue
        if contact and not args.force:
            continue
        candidates.append(
            {
                "record_id": str(record.get("record_id") or "").strip(),
                "influencer_id": influencer_id,
                "existing_contact": contact,
            }
        )

    if args.limit > 0:
        candidates = candidates[: args.limit]

    print(
        "scan "
        f"total_records={len(records)} existing_contact={existing_contact_count} "
        f"candidate_rows={len(candidates)} dry_run={args.dry_run}"
    )

    counts = {
        "processed": 0,
        "updated": 0,
        "would_update": 0,
        "no_contact": 0,
        "resolve_failed": 0,
        "fastmoss_failed": 0,
        "feishu_failed": 0,
    }
    min_delay = max(float(args.request_delay_min_seconds), 0.0)
    max_delay = max(float(args.request_delay_max_seconds), min_delay)

    with FastMossHTTPSession(
        phone=fastmoss_phone,
        password=fastmoss_password,
        request_delay_range=(min_delay, max_delay),
    ) as fastmoss:
        fastmoss.ensure_logged_in()
        for candidate in candidates:
            counts["processed"] += 1
            record_id = str(candidate["record_id"])
            influencer_id = str(candidate["influencer_id"])
            prefix = f"[{counts['processed']}/{len(candidates)}] {influencer_id}"

            try:
                uid, resolve_mode, result_count = resolve_author_uid(fastmoss, influencer_id)
                if not uid:
                    counts["resolve_failed"] += 1
                    print(f"{prefix} resolve_failed mode={resolve_mode} result_count={result_count}")
                    continue
                contact_payload = fastmoss.get_author_contact(uid)
                contact_text = first_contact_text(contact_payload)
                if not contact_text:
                    counts["no_contact"] += 1
                    print(f"{prefix} uid={uid} mode={resolve_mode} no_contact")
                    continue
            except FastMossHTTPError as exc:
                counts["fastmoss_failed"] += 1
                print(
                    f"{prefix} fastmoss_failed stage={exc.stage} "
                    f"code={exc.response_code} message={exc.message}"
                )
                continue
            except Exception as exc:
                counts["fastmoss_failed"] += 1
                print(f"{prefix} failed {type(exc).__name__}: {exc}")
                continue

            if args.dry_run:
                counts["would_update"] += 1
                print(f"{prefix} uid={uid} would_update contact={contact_text}")
                continue

            try:
                feishu.update_record(
                    table_meta["app_token"],
                    table_meta["table_id"],
                    record_id,
                    {args.contact_field_name: contact_text},
                )
                counts["updated"] += 1
                print(f"{prefix} uid={uid} updated contact={contact_text}")
                if args.feishu_update_delay_seconds > 0:
                    time.sleep(float(args.feishu_update_delay_seconds))
            except Exception as exc:
                counts["feishu_failed"] += 1
                print(f"{prefix} feishu_failed {type(exc).__name__}: {exc}")

            if max_delay > 0:
                time.sleep(random.uniform(min_delay, max_delay))

    print(
        "summary "
        + " ".join(f"{key}={value}" for key, value in counts.items())
    )
    return 1 if counts["fastmoss_failed"] or counts["feishu_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
