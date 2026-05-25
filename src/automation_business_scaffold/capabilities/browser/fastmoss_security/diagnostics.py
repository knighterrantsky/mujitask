from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from automation_business_scaffold.contracts.handler.shared import coerce_str, first_non_empty

DEFAULT_FASTMOSS_SLIDER_AUDIT_DIR = "runtime/downloads/fastmoss_slider_captcha_audit"

def _capture_fastmoss_browser_diagnostic_artifacts(
    page: Any,
    *,
    raw_page: Any | None,
    audit_dir: str,
    search_url: str,
    label: str,
    state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    root = Path(audit_dir or DEFAULT_FASTMOSS_SLIDER_AUDIT_DIR)
    run_key = hashlib.sha256(search_url.encode("utf-8")).hexdigest()[:16]
    target_dir = root / run_key / "browser_diagnostics"
    safe_label = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(label)).strip("_") or "browser_diagnostic"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        state_payload = {
            "label": safe_label,
            "search_url": search_url,
            "page_url": first_non_empty(getattr(page, "url", ""), getattr(raw_page, "url", "")),
            "page_title": _page_title(page) or _page_title(raw_page),
            "state": _json_safe_value(dict(state)),
        }
        refs = [
            _write_fastmoss_slider_json_file(
                target_dir / f"{safe_label}_state.json",
                state_payload,
                artifact_key=f"{safe_label}_state",
            )
        ]
        screenshot = _capture_page_screenshot_bytes(page) or _capture_page_screenshot_bytes(raw_page)
        if screenshot:
            refs.append(
                _write_fastmoss_slider_binary_file(
                    target_dir / f"{safe_label}_screenshot",
                    screenshot,
                    artifact_key=f"{safe_label}_screenshot",
                )
            )
        else:
            refs.append(
                _write_fastmoss_slider_json_file(
                    target_dir / f"{safe_label}_screenshot_unavailable.json",
                    {"reason": "page_screenshot_unavailable"},
                    artifact_key=f"{safe_label}_screenshot_unavailable",
                )
            )
        return refs
    except Exception as exc:  # noqa: BLE001
        return [
            {
                "artifact_key": f"{safe_label}_diagnostic_capture_failed",
                "error": str(exc),
                "mime_type": "application/json",
            }
        ]


def _capture_page_screenshot_bytes(page: Any | None) -> bytes:
    if page is None:
        return b""
    screenshot = getattr(page, "screenshot", None)
    if not callable(screenshot):
        return b""
    for kwargs in ({"full_page": True, "timeout": 3_000}, {"full_page": True}, {}):
        try:
            payload = screenshot(**kwargs)
        except TypeError:
            continue
        except Exception:
            return b""
        return payload if isinstance(payload, bytes) else b""
    return b""


def _page_title(page: Any | None) -> str:
    title = getattr(page, "title", None)
    if not callable(title):
        return ""
    try:
        return coerce_str(title())
    except Exception:
        return ""


def _json_safe_value(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return str(value)


def _persist_fastmoss_slider_artifacts_payload(
    artifacts_payload: Mapping[str, Any],
    *,
    audit_dir: str,
    search_url: str,
) -> list[dict[str, Any]]:
    root = Path(audit_dir or DEFAULT_FASTMOSS_SLIDER_AUDIT_DIR)
    run_key = hashlib.sha256(search_url.encode("utf-8")).hexdigest()[:16]
    target_dir = root / run_key
    target_dir.mkdir(parents=True, exist_ok=True)
    refs: list[dict[str, Any]] = []
    state_dump = artifacts_payload.get("state_dump")
    if state_dump:
        refs.append(_write_fastmoss_slider_json_file(target_dir / "slider_captcha_audit.json", state_dump))

    extra = artifacts_payload.get("extra") if isinstance(artifacts_payload.get("extra"), Mapping) else {}
    for key, value in extra.items():
        safe_key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(key)).strip("_") or "artifact"
        if isinstance(value, bytes):
            refs.append(_write_fastmoss_slider_binary_file(target_dir / f"{safe_key}.bin", value, artifact_key=str(key)))
        elif key == "slider_captcha_audit":
            continue
        elif isinstance(value, (dict, list, str, int, float, bool)) or value is None:
            refs.append(_write_fastmoss_slider_json_file(target_dir / f"{safe_key}.json", value, artifact_key=str(key)))
    return refs


def _write_fastmoss_slider_json_file(
    path: Path,
    value: Any,
    *,
    artifact_key: str = "slider_captcha_audit",
) -> dict[str, Any]:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "artifact_key": artifact_key,
        "local_path": str(path),
        "file_name": path.name,
        "mime_type": "application/json",
    }


def _write_fastmoss_slider_binary_file(path: Path, value: bytes, *, artifact_key: str) -> dict[str, Any]:
    suffix = _fastmoss_slider_binary_suffix(value)
    final_path = path.with_suffix(suffix)
    final_path.write_bytes(value)
    return {
        "artifact_key": artifact_key,
        "local_path": str(final_path),
        "file_name": final_path.name,
        "mime_type": _fastmoss_slider_binary_mime_type(suffix),
    }


def _fastmoss_slider_binary_suffix(value: bytes) -> str:
    if value.startswith(b"\x89PNG"):
        return ".png"
    if value.startswith(b"\xff\xd8"):
        return ".jpg"
    if value.startswith(b"RIFF") and value[8:12] == b"WEBP":
        return ".webp"
    if value.startswith(b"GIF87a") or value.startswith(b"GIF89a"):
        return ".gif"
    return ".bin"


def _fastmoss_slider_binary_mime_type(suffix: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "application/octet-stream")
