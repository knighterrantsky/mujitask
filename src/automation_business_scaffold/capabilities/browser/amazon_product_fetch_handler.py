from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from automation_business_scaffold.capabilities.browser.amazon.product_page import (
    AmazonAccessBlockedError,
    AmazonIdentityMismatchError,
    AmazonProductExtractionError,
    InvalidASINError,
    canonical_amazon_url,
    extract_amazon_product_capture,
    normalize_asin,
)
from automation_business_scaffold.config import get_execution_control_defaults
from automation_business_scaffold.contracts.handler.allowlist import BROWSER_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    failed_result,
    partial_success_result,
    success_result,
)
from automation_business_scaffold.infrastructure.artifacts.artifact_store import (
    create_artifact_store,
    join_object_key,
    normalize_artifact_store_provider,
)
from automation_business_scaffold.infrastructure.browser.browser_bridge import open_automation_page


HANDLER_CODE = "amazon_product_browser_fetch"
CONTRACT = BROWSER_HANDLER_CONTRACTS[HANDLER_CODE]
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SCRIPT_PATTERN = re.compile(
    r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SENSITIVE_ATTRIBUTE_PATTERN = re.compile(
    r"\s+(?:data-)?(?:cookie|authorization|auth-token|access-token|refresh-token|token|"
    r"localstorage|workspace|profile|account|address|request-headers?)\s*=\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s>]+)",
    re.IGNORECASE,
)
_SENSITIVE_ELEMENT_PATTERN = re.compile(
    r"<(?P<tag>div|span|a|section|li)\b[^>]*(?:nav-link-accountlist|"
    r"nav-global-location|glow-ingress|account-name|address-book)[^>]*>.*?"
    r"</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SENSITIVE_JSON_KEYS = {
    "account",
    "address",
    "authorization",
    "cookie",
    "cookies",
    "headers",
    "localstorage",
    "profile",
    "refreshtoken",
    "requestheaders",
    "token",
    "workspace",
}


def amazon_product_browser_fetch_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    try:
        requested_asin = normalize_asin(payload.get("requested_asin"))
    except InvalidASINError as exc:
        return _failure(
            context,
            error_code=exc.error_code,
            message=str(exc),
            retryable=False,
            collection_status="failed",
        )

    try:
        source_record_id = _required_text(payload.get("source_record_id"), "source_record_id")
        run_id = _safe_segment(payload.get("run_id"), "run_id")
    except ValueError as exc:
        return _failure(
            context,
            error_code="invalid_browser_request",
            message=str(exc),
            retryable=False,
            collection_status="failed",
        )

    artifact_policy = _resolve_artifact_policy(context)
    if artifact_policy is None:
        return _failure(
            context,
            error_code="object_storage_required",
            message=(
                "amazon_product_browser_fetch requires configured non-local object storage."
            ),
            retryable=False,
            collection_status="failed",
        )

    profile_ref = _profile_ref()
    if not profile_ref:
        return _failure(
            context,
            error_code="browser_profile_unavailable",
            message=(
                "Set AMAZON_US_BROWSER_PROFILE_REF or DEFAULT_PROFILE_REF before collecting "
                "Amazon product pages."
            ),
            retryable=False,
            collection_status="failed",
        )

    observed_at = _observed_at(context.metadata.get("observed_at"))
    canonical_url = canonical_amazon_url(requested_asin)
    try:
        collection = _collect_browser_page(
            requested_asin=requested_asin,
            canonical_url=canonical_url,
            profile_ref=profile_ref,
            observed_at=observed_at,
            timeout_ms=_browser_timeout_ms(),
        )
    except Exception as exc:  # pragma: no cover - provider boundary
        return _failure(
            context,
            error_code="transient_page_failure",
            message=str(exc),
            retryable=True,
            collection_status="failed",
        )

    browser_target_digest = _clean_text(collection.get("browser_target_digest"))
    error = collection.get("error")
    if error is not None:
        error_code, message, retryable, collection_status = _page_error(error)
        screenshot_bytes = _bytes_value(collection.get("screenshot_bytes"))
        try:
            artifact_refs = _write_failure_artifacts(
                context=context,
                artifact_policy=artifact_policy,
                requested_asin=requested_asin,
                run_id=run_id,
                observed_at=observed_at,
                html=_clean_text(collection.get("html"), strip=False),
                screenshot_bytes=screenshot_bytes,
            )
        except Exception as exc:
            return _failure(
                context,
                error_code="artifact_write_failed",
                message=str(exc),
                retryable=True,
                collection_status="failed",
                browser_target_digest=browser_target_digest,
            )
        if not screenshot_bytes:
            return _failure(
                context,
                error_code="required_failure_evidence_missing",
                message="Amazon failed or blocked page capture requires screenshot evidence.",
                retryable=True,
                collection_status="failed",
                browser_target_digest=browser_target_digest,
                artifact_refs=artifact_refs,
                requested_asin=requested_asin,
                canonical_url=canonical_url,
            )
        return _failure(
            context,
            error_code=error_code,
            message=message,
            retryable=retryable,
            collection_status=collection_status,
            browser_target_digest=browser_target_digest,
            artifact_refs=artifact_refs,
            requested_asin=requested_asin,
            canonical_url=canonical_url,
        )

    capture = collection.get("capture")
    if not isinstance(capture, dict):
        return _failure(
            context,
            error_code="invalid_amazon_capture",
            message="Amazon browser collection did not return a normalized capture.",
            retryable=False,
            collection_status="failed",
            browser_target_digest=browser_target_digest,
        )
    capture = dict(capture)
    if capture.get("requested_asin") != requested_asin:
        return _failure(
            context,
            error_code="identity_mismatch",
            message="Normalized capture requested_asin does not match the browser job.",
            retryable=False,
            collection_status="failed",
            browser_target_digest=browser_target_digest,
        )
    capture["profile_context"] = _profile_context(browser_target_digest)
    capture_bytes = json.dumps(
        capture,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    sanitized_html = _sanitize_amazon_html(
        _clean_text(collection.get("html"), strip=False)
    )
    try:
        normalized_ref, html_ref = _write_success_artifacts(
            context=context,
            artifact_policy=artifact_policy,
            requested_asin=requested_asin,
            run_id=run_id,
            observed_at=observed_at,
            capture_bytes=capture_bytes,
            sanitized_html=sanitized_html,
        )
    except Exception as exc:
        return _failure(
            context,
            error_code="artifact_write_failed",
            message=str(exc),
            retryable=True,
            collection_status="failed",
            browser_target_digest=browser_target_digest,
        )

    coverage = _field_coverage(capture.get("field_evidence"))
    collection_status = _clean_text(capture.get("collection_status"))
    result = {
        "marketplace_code": "US",
        "requested_asin": requested_asin,
        "resolved_asin": _clean_text(capture.get("resolved_asin")),
        "canonical_url": canonical_url,
        "collection_status": collection_status,
        "field_coverage": coverage,
        "normalized_capture_ref": normalized_ref,
        "raw_capture_refs": [normalized_ref, html_ref],
        "artifact_refs": [normalized_ref, html_ref],
        "media_source_refs": _media_source_refs(capture),
        "browser_target_digest": browser_target_digest,
    }
    summary = {
        "marketplace_code": "US",
        "requested_asin": requested_asin,
        "resolved_asin": result["resolved_asin"],
        "source_record_id": source_record_id,
        "collection_status": collection_status,
        "coverage_percent": coverage["percentage"],
        "artifact_count": 2,
        "browser_target_digest": browser_target_digest,
    }
    if collection_status == "partial_success":
        return partial_success_result(
            context,
            summary=summary,
            result=result,
            warnings=("Amazon page capture contains missing optional fields.",),
        )
    return success_result(context, summary=summary, result=result)


def _resolve_artifact_policy(context: HandlerContext) -> dict[str, Any] | None:
    defaults = get_execution_control_defaults()
    store = context.metadata.get("artifact_store")
    if store is None:
        provider = normalize_artifact_store_provider(defaults.artifact_store_provider)
        if provider == "local" or not defaults.artifact_bucket:
            return None
        try:
            store = create_artifact_store(
                {
                    "artifact_store_provider": defaults.artifact_store_provider,
                    "minio_endpoint": defaults.minio_endpoint,
                    "minio_access_key": defaults.minio_access_key,
                    "minio_secret_key": defaults.minio_secret_key,
                    "minio_secure": defaults.minio_secure,
                    "minio_region": defaults.minio_region,
                    "minio_create_bucket": defaults.minio_create_bucket,
                }
            )
        except (RuntimeError, ValueError):
            return None
    if store is None or not callable(getattr(store, "upload_file", None)):
        return None
    if normalize_artifact_store_provider(getattr(store, "provider_code", "")) == "local":
        return None
    bucket = _clean_text(
        context.metadata.get("artifact_bucket")
        or getattr(store, "artifact_bucket", "")
        or defaults.artifact_bucket
    )
    if not bucket:
        return None
    object_prefix = _clean_text(
        context.metadata.get("artifact_object_prefix")
        or getattr(store, "artifact_object_prefix", "")
        or defaults.artifact_object_prefix
    ).strip("/")
    return {"store": store, "bucket": bucket, "object_prefix": object_prefix}


def _profile_ref() -> str:
    return _clean_text(
        os.environ.get("AMAZON_US_BROWSER_PROFILE_REF")
        or os.environ.get("DEFAULT_PROFILE_REF")
    )


def _profile_context(browser_target_digest: str) -> dict[str, str]:
    locale = _clean_text(os.environ.get("AMAZON_US_LOCALE")) or "en_US"
    delivery_region = _clean_text(os.environ.get("AMAZON_US_DELIVERY_REGION")) or "US"
    digest_seed = f"{browser_target_digest}:{locale}:USD:{delivery_region}"
    return {
        "locale": locale,
        "currency": "USD",
        "delivery_region": delivery_region,
        "profile_context_digest": hashlib.sha256(digest_seed.encode("utf-8")).hexdigest(),
    }


def _collect_browser_page(
    *,
    requested_asin: str,
    canonical_url: str,
    profile_ref: str,
    observed_at: datetime,
    timeout_ms: int,
) -> dict[str, Any]:
    with open_automation_page(profile_ref=profile_ref) as browser_session:
        page = browser_session.page
        target_digest = hashlib.sha256(
            browser_session.target_key.encode("utf-8")
        ).hexdigest()
        try:
            _navigate(page, canonical_url, timeout_ms=timeout_ms)
            _wait_for_amazon_page(page, timeout_ms=timeout_ms)
            html = _page_content(page)
            if not html.strip():
                raise RuntimeError("Amazon browser page content is empty or unreadable.")
            resolved_url = _clean_text(getattr(page, "url", "")) or canonical_url
            capture = extract_amazon_product_capture(
                html,
                requested_asin=requested_asin,
                resolved_url=resolved_url,
                observed_at=observed_at,
            )
            return {
                "capture": capture,
                "html": html,
                "resolved_url": resolved_url,
                "browser_target_digest": target_digest,
                "screenshot_bytes": b"",
            }
        except AmazonProductExtractionError as exc:
            return {
                "capture": None,
                "html": _page_content(page),
                "resolved_url": _clean_text(getattr(page, "url", "")) or canonical_url,
                "browser_target_digest": target_digest,
                "screenshot_bytes": _page_screenshot(page),
                "error": exc,
            }
        except Exception as exc:  # browser/provider error boundary
            error_code = (
                "navigation_timeout"
                if "timeout" in type(exc).__name__.lower() or "timeout" in str(exc).lower()
                else "transient_page_failure"
            )
            return {
                "capture": None,
                "html": _page_content(page),
                "resolved_url": _clean_text(getattr(page, "url", "")) or canonical_url,
                "browser_target_digest": target_digest,
                "screenshot_bytes": _page_screenshot(page),
                "error": {
                    "error_code": error_code,
                    "message": str(exc),
                    "retryable": True,
                    "collection_status": "failed",
                },
            }


def _navigate(page: Any, url: str, *, timeout_ms: int) -> None:
    goto = getattr(page, "goto", None)
    if not callable(goto):
        raise RuntimeError("Browser page does not support navigation.")
    try:
        goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except TypeError:
        goto(url)


def _wait_for_amazon_page(page: Any, *, timeout_ms: int) -> None:
    wait_for_load_state = getattr(page, "wait_for_load_state", None)
    if callable(wait_for_load_state):
        try:
            wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except TypeError:
            wait_for_load_state("domcontentloaded")
    wait_for_selector = getattr(page, "wait_for_selector", None)
    if callable(wait_for_selector):
        try:
            wait_for_selector(
                "#productTitle, #availability, form[action*='validateCaptcha'], title",
                timeout=min(timeout_ms, 10_000),
            )
        except Exception:
            pass
    evaluate = getattr(page, "evaluate", None)
    if callable(evaluate):
        try:
            evaluate("window.scrollTo(0, Math.min(document.body.scrollHeight, 1800))")
        except Exception:
            pass


def _page_content(page: Any) -> str:
    content = getattr(page, "content", None)
    if not callable(content):
        return ""
    try:
        return str(content())
    except Exception:
        return ""


def _page_screenshot(page: Any) -> bytes:
    screenshot = getattr(page, "screenshot", None)
    if not callable(screenshot):
        return b""
    try:
        evaluate = getattr(page, "evaluate", None)
        if callable(evaluate):
            try:
                evaluate(
                    """
                    document.querySelectorAll(
                      '#nav-link-accountList, #nav-global-location-slot, '
                      + '#glow-ingress-block, [data-nav-role="signin"], '
                      + '[id*="address-book"], [class*="account-name"]'
                    ).forEach((node) => { node.style.visibility = 'hidden'; });
                    """
                )
            except Exception:
                pass
        return _bytes_value(screenshot(full_page=True))
    except Exception:
        return b""


def _write_success_artifacts(
    *,
    context: HandlerContext,
    artifact_policy: Mapping[str, Any],
    requested_asin: str,
    run_id: str,
    observed_at: datetime,
    capture_bytes: bytes,
    sanitized_html: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    base_key = _raw_capture_base_key(
        artifact_policy,
        requested_asin=requested_asin,
        run_id=run_id,
        observed_at=observed_at,
    )
    normalized_ref = _upload_bytes(
        context=context,
        artifact_policy=artifact_policy,
        object_key=f"{base_key}/normalized.json",
        payload=capture_bytes,
        capture_kind="normalized_capture",
        content_type="application/json",
        sanitization_status="normalized",
        observed_at=observed_at,
    )
    html_ref = _upload_bytes(
        context=context,
        artifact_policy=artifact_policy,
        object_key=f"{base_key}/page.html.gz",
        payload=gzip.compress(sanitized_html.encode("utf-8"), mtime=0),
        capture_kind="html",
        content_type="application/gzip",
        sanitization_status="sanitized",
        observed_at=observed_at,
        content_encoding="gzip",
    )
    return normalized_ref, html_ref


def _write_failure_artifacts(
    *,
    context: HandlerContext,
    artifact_policy: Mapping[str, Any],
    requested_asin: str,
    run_id: str,
    observed_at: datetime,
    html: str,
    screenshot_bytes: bytes,
) -> list[dict[str, Any]]:
    base_key = _raw_capture_base_key(
        artifact_policy,
        requested_asin=requested_asin,
        run_id=run_id,
        observed_at=observed_at,
    )
    refs: list[dict[str, Any]] = []
    if html:
        refs.append(
            _upload_bytes(
                context=context,
                artifact_policy=artifact_policy,
                object_key=f"{base_key}/page.html.gz",
                payload=gzip.compress(_sanitize_amazon_html(html).encode("utf-8"), mtime=0),
                capture_kind="html",
                content_type="application/gzip",
                sanitization_status="sanitized",
                observed_at=observed_at,
                content_encoding="gzip",
            )
        )
    if screenshot_bytes:
        refs.append(
            _upload_bytes(
                context=context,
                artifact_policy=artifact_policy,
                object_key=f"{base_key}/page.png",
                payload=screenshot_bytes,
                capture_kind="screenshot",
                content_type="image/png",
                sanitization_status="not_applicable",
                observed_at=observed_at,
            )
        )
    return refs


def _raw_capture_base_key(
    artifact_policy: Mapping[str, Any],
    *,
    requested_asin: str,
    run_id: str,
    observed_at: datetime,
) -> str:
    relative = (
        f"raw-captures/amazon/us/{requested_asin}/"
        f"{observed_at:%Y/%m/%d}/{run_id}"
    )
    return join_object_key(_clean_text(artifact_policy.get("object_prefix")), relative)


def _upload_bytes(
    *,
    context: HandlerContext,
    artifact_policy: Mapping[str, Any],
    object_key: str,
    payload: bytes,
    capture_kind: str,
    content_type: str,
    sanitization_status: str,
    observed_at: datetime,
    content_encoding: str = "",
) -> dict[str, Any]:
    store = artifact_policy["store"]
    bucket = _required_text(artifact_policy.get("bucket"), "artifact bucket")
    with tempfile.TemporaryDirectory(prefix="mujitask-amazon-") as temp_dir:
        local_path = Path(temp_dir) / Path(object_key).name
        local_path.write_bytes(payload)
        stored = store.upload_file(
            bucket=bucket,
            object_key=object_key,
            local_path=local_path,
            content_type=content_type,
            metadata={
                "request_id": context.request_id,
                "execution_id": context.job_id,
                "capture_kind": capture_kind,
                "source_platform": "amazon",
                "marketplace_code": "US",
            },
        )
    if stored.bucket != bucket or stored.object_key != object_key:
        raise ValueError("Artifact store returned coordinates outside the requested Amazon key.")
    ref = {
        "capture_kind": capture_kind,
        "bucket": stored.bucket,
        "object_key": stored.object_key,
        "content_digest": hashlib.sha256(payload).hexdigest(),
        "content_type": content_type,
        "sanitization_status": sanitization_status,
        "collected_at": _iso_timestamp(observed_at),
        "created_at": _iso_timestamp(observed_at),
        "created_at_epoch": observed_at.timestamp(),
        "etag": _clean_text(stored.etag),
        "size": int(stored.size),
        "remote_uri": _clean_text(stored.uri),
    }
    if content_encoding:
        ref["content_encoding"] = content_encoding
    return ref


def _sanitize_amazon_html(html: str) -> str:
    if not html:
        return ""

    def sanitize_script(match: re.Match[str]) -> str:
        attrs = match.group("attrs")
        lowered = attrs.lower()
        keep = "application/ld+json" in lowered or "amazon-product-state" in lowered
        if not keep:
            return ""
        try:
            value = json.loads(match.group("body"))
        except (TypeError, ValueError):
            return ""
        safe_value = _sanitize_json_value(value)
        safe_attrs: list[str] = []
        if "amazon-product-state" in lowered:
            safe_attrs.append('id="amazon-product-state"')
            safe_attrs.append('type="application/json"')
        else:
            safe_attrs.append('type="application/ld+json"')
        return (
            f"<script {' '.join(safe_attrs)}>"
            f"{json.dumps(safe_value, ensure_ascii=False, separators=(',', ':'))}"
            "</script>"
        )

    sanitized = _SCRIPT_PATTERN.sub(sanitize_script, html)
    sanitized = re.sub(r"<!--.*?-->", "", sanitized, flags=re.DOTALL)
    for _ in range(3):
        sanitized = _SENSITIVE_ELEMENT_PATTERN.sub("", sanitized)
    sanitized = re.sub(r"<input\b[^>]*>", "", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(
        r"<meta\b[^>]*(?:http-equiv|authorization|cookie|token)[^>]*>",
        "",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = _SENSITIVE_ATTRIBUTE_PATTERN.sub("", sanitized)
    sanitized = re.sub(
        r"\bBearer\s+[A-Za-z0-9._~+/-]+=*",
        "Bearer [REDACTED]",
        sanitized,
        flags=re.IGNORECASE,
    )
    return sanitized


def _sanitize_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in _SENSITIVE_JSON_KEYS or normalized.endswith("token") or any(
                marker in normalized
                for marker in (
                    "authorization",
                    "cookie",
                    "localstorage",
                    "requestheader",
                )
            ):
                continue
            result[str(key)] = _sanitize_json_value(item)
        return result
    if isinstance(value, list):
        return [_sanitize_json_value(item) for item in value]
    return value


def _field_coverage(value: Any) -> dict[str, int | float]:
    evidence = value if isinstance(value, dict) else {}
    statuses = [
        item.get("status")
        for item in evidence.values()
        if isinstance(item, dict)
    ]
    observed = statuses.count("observed")
    explicitly_unavailable = statuses.count("explicitly_unavailable")
    missing = statuses.count("missing")
    total = len(statuses)
    covered = observed + explicitly_unavailable
    return {
        "total": total,
        "observed": observed,
        "explicitly_unavailable": explicitly_unavailable,
        "missing": missing,
        "percentage": round((covered / total) * 100.0, 2) if total else 0.0,
    }


def _media_source_refs(capture: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence = capture.get("field_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    media = capture.get("media")
    media = media if isinstance(media, dict) else {}
    requested_asin = _clean_text(capture.get("requested_asin"))
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def append(item: Any, *, role: str, position: int) -> None:
        source_url = _media_url(item)
        if not source_url or source_url in seen:
            return
        seen.add(source_url)
        refs.append(
            {
                "source_url": source_url,
                "source_platform": "amazon",
                "marketplace_code": "US",
                "product_id": requested_asin,
                "media_role": role,
                "position": position,
            }
        )

    if _evidence_status(evidence, "media.main_image") == "observed":
        append(media.get("main_image"), role="main_image", position=0)
    if _evidence_status(evidence, "media.gallery_images") == "observed":
        gallery = media.get("gallery_images")
        if isinstance(gallery, list):
            for index, item in enumerate(gallery):
                append(item, role="gallery_image", position=index)
    return refs


def _media_url(value: Any) -> str:
    if isinstance(value, dict):
        return _clean_text(value.get("url"))
    return ""


def _evidence_status(evidence: Mapping[str, Any], path: str) -> str:
    item = evidence.get(path)
    return _clean_text(item.get("status")) if isinstance(item, dict) else ""


def _page_error(error: Any) -> tuple[str, str, bool, str]:
    if isinstance(error, Mapping):
        return (
            _clean_text(error.get("error_code")) or "transient_page_failure",
            _clean_text(error.get("message")) or "Amazon browser collection failed.",
            bool(error.get("retryable")),
            _clean_text(error.get("collection_status")) or "failed",
        )
    if isinstance(error, AmazonAccessBlockedError):
        return error.error_code, str(error), False, "blocked"
    if isinstance(error, AmazonIdentityMismatchError):
        return error.error_code, str(error), False, "failed"
    if isinstance(error, AmazonProductExtractionError):
        return error.error_code, str(error), False, "failed"
    return "transient_page_failure", str(error), True, "failed"


def _failure(
    context: HandlerContext,
    *,
    error_code: str,
    message: str,
    retryable: bool,
    collection_status: str,
    browser_target_digest: str = "",
    artifact_refs: list[dict[str, Any]] | None = None,
    requested_asin: str = "",
    canonical_url: str = "",
) -> HandlerResult:
    refs = list(artifact_refs or [])
    result: dict[str, Any] = {
        "collection_status": collection_status,
        "artifact_refs": refs,
        "raw_capture_refs": refs,
    }
    if requested_asin:
        result["marketplace_code"] = "US"
        result["requested_asin"] = requested_asin
    if canonical_url:
        result["canonical_url"] = canonical_url
    if browser_target_digest:
        result["browser_target_digest"] = browser_target_digest
    return failed_result(
        context,
        error=build_error(
            error_type="amazon_browser_failure",
            error_code=error_code,
            message=message,
            retryable=retryable,
            details={
                "collection_status": collection_status,
                "artifact_count": len(refs),
            },
        ),
        summary={
            "marketplace_code": "US",
            "requested_asin": requested_asin,
            "collection_status": collection_status,
            "artifact_count": len(refs),
            "browser_target_digest": browser_target_digest,
        },
        result=result,
    )


def _browser_timeout_ms() -> int:
    raw = _clean_text(os.environ.get("AMAZON_US_BROWSER_TIMEOUT_MS"))
    try:
        return max(int(raw), 1_000) if raw else 30_000
    except ValueError:
        return 30_000


def _observed_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_segment(value: Any, name: str) -> str:
    text = _required_text(value, name)
    if ".." in text or not _SAFE_SEGMENT.fullmatch(text):
        raise ValueError(f"{name} must be a safe object-key segment.")
    return text


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required.")
    return value.strip()


def _clean_text(value: Any, *, strip: bool = True) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip() if strip else value


def _bytes_value(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    return b""


__all__ = ["CONTRACT", "HANDLER_CODE", "amazon_product_browser_fetch_handler"]
