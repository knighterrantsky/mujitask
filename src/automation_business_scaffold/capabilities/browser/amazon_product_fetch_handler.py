from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from automation_business_scaffold.capabilities.browser.amazon.product_page import (
    AmazonAccessBlockedError,
    AmazonIdentityMismatchError,
    AmazonProductExtractionError,
    InvalidASINError,
    amazon_network_product_data_asin,
    canonical_amazon_url,
    extract_amazon_network_product_data,
    extract_amazon_product_capture,
    extract_asin_from_url,
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
_SENSITIVE_HTML_MARKERS = (
    "accountname",
    "addressbook",
    "customername",
    "deliveryaddress",
    "glowingress",
    "navgloballocation",
    "navlinkaccountlist",
    "shippingaddress",
)
_SENSITIVE_HTML_ATTRIBUTE_NAMES = {
    "account",
    "address",
    "authorization",
    "cookie",
    "localstorage",
    "profile",
    "token",
    "workspace",
}
_DROPPED_HTML_TAGS = {
    "button",
    "form",
    "iframe",
    "input",
    "meta",
    "noscript",
    "option",
    "select",
    "style",
    "textarea",
}
_VOID_HTML_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_SAFE_HTML_ATTRIBUTES = {
    "class",
    "data-asin",
    "data-feature-name",
    "id",
    "itemprop",
    "itemscope",
    "itemtype",
    "type",
}
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
_MAX_NETWORK_RESPONSE_COUNT = 8
_MAX_NETWORK_RESPONSE_BYTES = 256 * 1024
_MAX_NETWORK_TOTAL_BYTES = 512 * 1024
_NETWORK_SETTLE_MS = 750
_BROWSER_PROVIDER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BROWSER_STAGE_NAMES = ("navigation", "parse", "artifact")


class _ArtifactWriteError(RuntimeError):
    def __init__(self, message: str, *, artifact_refs: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.artifact_refs = list(artifact_refs)


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
            message=("amazon_product_browser_fetch requires configured non-local object storage."),
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
    browser_provider_name = _browser_provider_name(collection.get("browser_provider_name"))
    stage_durations_ms = _browser_stage_durations(collection.get("stage_durations_ms"))
    error = collection.get("error")
    if error is not None:
        error_code, message, retryable, collection_status = _page_error(error)
        screenshot_bytes = _bytes_value(collection.get("screenshot_bytes"))
        artifact_started_at = perf_counter()
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
            stage_durations_ms["artifact"] = _elapsed_ms(artifact_started_at)
            return _failure(
                context,
                error_code="artifact_write_failed",
                message=str(exc),
                retryable=True,
                collection_status="failed",
                browser_target_digest=browser_target_digest,
                artifact_refs=(
                    exc.artifact_refs if isinstance(exc, _ArtifactWriteError) else []
                ),
                browser_provider_name=browser_provider_name,
                stage_durations_ms=stage_durations_ms,
            )
        stage_durations_ms["artifact"] = _elapsed_ms(artifact_started_at)
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
                browser_provider_name=browser_provider_name,
                stage_durations_ms=stage_durations_ms,
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
            browser_provider_name=browser_provider_name,
            stage_durations_ms=stage_durations_ms,
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
            browser_provider_name=browser_provider_name,
            stage_durations_ms=stage_durations_ms,
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
            browser_provider_name=browser_provider_name,
            stage_durations_ms=stage_durations_ms,
        )
    capture["profile_context"] = _profile_context(browser_target_digest)
    _normalize_capture_media_urls(capture)
    try:
        network_data = extract_amazon_network_product_data(
            [
                {
                    "source_path": "/page-data",
                    "payload": collection.get("network_data"),
                }
            ],
            expected_asin=capture.get("resolved_asin"),
        )
    except InvalidASINError:
        network_data = {}
    capture_bytes = json.dumps(
        capture,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    sanitized_html = _sanitize_amazon_html(_clean_text(collection.get("html"), strip=False))
    artifact_started_at = perf_counter()
    try:
        normalized_ref, html_ref, network_ref = _write_success_artifacts(
            context=context,
            artifact_policy=artifact_policy,
            requested_asin=requested_asin,
            run_id=run_id,
            observed_at=observed_at,
            capture_bytes=capture_bytes,
            sanitized_html=sanitized_html,
            network_data=network_data,
        )
    except Exception as exc:
        stage_durations_ms["artifact"] = _elapsed_ms(artifact_started_at)
        return _failure(
            context,
            error_code="artifact_write_failed",
            message=str(exc),
            retryable=True,
            collection_status="failed",
            browser_target_digest=browser_target_digest,
            artifact_refs=(
                exc.artifact_refs if isinstance(exc, _ArtifactWriteError) else []
            ),
            browser_provider_name=browser_provider_name,
            stage_durations_ms=stage_durations_ms,
        )
    stage_durations_ms["artifact"] = _elapsed_ms(artifact_started_at)

    artifact_refs = [normalized_ref, html_ref]
    if network_ref:
        artifact_refs.append(network_ref)
    coverage = _field_coverage(capture.get("field_evidence"))
    collection_status = _clean_text(capture.get("collection_status"))
    variants = capture.get("variants")
    parent_asin = _clean_text(variants.get("parent_asin")) if isinstance(variants, Mapping) else ""
    media_source_refs = _media_source_refs(capture)
    result = {
        "marketplace_code": "US",
        "requested_asin": requested_asin,
        "resolved_asin": _clean_text(capture.get("resolved_asin")),
        "canonical_url": canonical_url,
        "collection_status": collection_status,
        "field_coverage": coverage,
        "normalized_capture_ref": normalized_ref,
        "raw_capture_refs": artifact_refs,
        "artifact_refs": artifact_refs,
        "media_source_refs": media_source_refs,
        "browser_target_digest": browser_target_digest,
    }
    if browser_provider_name:
        result["browser_provider_name"] = browser_provider_name
    if stage_durations_ms:
        result["stage_durations_ms"] = stage_durations_ms
    if parent_asin and result["resolved_asin"] != requested_asin:
        result["parent_asin"] = parent_asin
    summary = {
        "marketplace_code": "US",
        "requested_asin": requested_asin,
        "resolved_asin": result["resolved_asin"],
        "source_record_id": source_record_id,
        "collection_status": collection_status,
        "coverage_percent": coverage["percentage"],
        "artifact_count": len(artifact_refs),
        "media_observed_count": len(media_source_refs),
        "browser_target_digest": browser_target_digest,
    }
    if browser_provider_name:
        summary["browser_provider_name"] = browser_provider_name
    if stage_durations_ms:
        summary["stage_durations_ms"] = stage_durations_ms
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
        os.environ.get("AMAZON_US_BROWSER_PROFILE_REF") or os.environ.get("DEFAULT_PROFILE_REF")
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
        automation_page = browser_session.page
        page = getattr(browser_session, "raw_page", None) or automation_page
        network_observations: list[dict[str, Any]] = []
        stop_observing = _observe_same_origin_product_responses(
            page,
            canonical_url=canonical_url,
            observations=network_observations,
        )
        target_digest = hashlib.sha256(browser_session.target_key.encode("utf-8")).hexdigest()
        browser_provider_name = _browser_provider_name(
            getattr(browser_session, "provider_name", "")
        )
        stage_durations_ms: dict[str, float] = {}
        try:
            try:
                navigation_started_at = perf_counter()
                try:
                    navigation_response = _navigate(
                        automation_page,
                        canonical_url,
                        timeout_ms=timeout_ms,
                    )
                    navigation_error = _navigation_response_error(navigation_response)
                    if navigation_error:
                        return {
                            "capture": None,
                            "html": _page_content(page),
                            "resolved_url": (
                                _clean_text(getattr(page, "url", "")) or canonical_url
                            ),
                            "browser_target_digest": target_digest,
                            "browser_provider_name": browser_provider_name,
                            "stage_durations_ms": stage_durations_ms,
                            "screenshot_bytes": _page_screenshot(page),
                            "error": navigation_error,
                        }
                    _wait_for_amazon_page(page, timeout_ms=timeout_ms)
                    _settle_network_responses(page)
                finally:
                    stage_durations_ms["navigation"] = _elapsed_ms(navigation_started_at)
                parse_started_at = perf_counter()
                try:
                    html = _page_content(page)
                    if not html.strip():
                        raise RuntimeError("Amazon browser page content is empty or unreadable.")
                    resolved_url = _clean_text(getattr(page, "url", "")) or canonical_url
                    try:
                        resolved_asin = extract_asin_from_url(resolved_url)
                    except AmazonProductExtractionError:
                        network_data = {}
                    else:
                        try:
                            network_data = extract_amazon_network_product_data(
                                network_observations,
                                expected_asin=resolved_asin,
                            )
                        except (
                            AmazonProductExtractionError,
                            TypeError,
                            ValueError,
                            OverflowError,
                        ):
                            network_data = {}
                    capture = extract_amazon_product_capture(
                        html,
                        requested_asin=requested_asin,
                        resolved_url=resolved_url,
                        observed_at=observed_at,
                        network_product_data=network_data,
                    )
                finally:
                    stage_durations_ms["parse"] = _elapsed_ms(parse_started_at)
                return {
                    "capture": capture,
                    "html": html,
                    "network_data": network_data,
                    "resolved_url": resolved_url,
                    "browser_target_digest": target_digest,
                    "browser_provider_name": browser_provider_name,
                    "stage_durations_ms": stage_durations_ms,
                    "screenshot_bytes": b"",
                }
            except AmazonProductExtractionError as exc:
                return {
                    "capture": None,
                    "html": _page_content(page),
                    "resolved_url": _clean_text(getattr(page, "url", "")) or canonical_url,
                    "browser_target_digest": target_digest,
                    "browser_provider_name": browser_provider_name,
                    "stage_durations_ms": stage_durations_ms,
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
                    "browser_provider_name": browser_provider_name,
                    "stage_durations_ms": stage_durations_ms,
                    "screenshot_bytes": _page_screenshot(page),
                    "error": {
                        "error_code": error_code,
                        "message": str(exc),
                        "retryable": True,
                        "collection_status": "failed",
                    },
                }
        finally:
            stop_observing()


def _observe_same_origin_product_responses(
    page: Any,
    *,
    canonical_url: str,
    observations: list[dict[str, Any]],
) -> Callable[[], None]:
    on = getattr(page, "on", None)
    if not callable(on):
        return lambda: None

    total_bytes = 0
    observation_sizes: list[int] = []

    def observe(response: Any) -> None:
        nonlocal total_bytes
        try:
            response_url = _clean_text(getattr(response, "url", ""))
            if not _is_exact_same_origin(response_url, canonical_url):
                return
            request = getattr(response, "request", None)
            resource_type = _clean_text(getattr(request, "resource_type", "")).lower()
            if resource_type not in {"fetch", "xhr"}:
                return
            headers = _response_headers(response)
            content_type = _clean_text(headers.get("content-type")).lower()
            media_type = content_type.split(";", 1)[0].strip()
            if media_type != "application/json" and not media_type.endswith("+json"):
                return
            content_length = _response_content_length(headers)
            if content_length > _MAX_NETWORK_RESPONSE_BYTES:
                return
            body = _response_body(response)
            if not body or len(body) > _MAX_NETWORK_RESPONSE_BYTES:
                return
            payload = json.loads(body, parse_constant=_reject_json_constant)
            if not isinstance(payload, Mapping):
                return
            payload_asin = amazon_network_product_data_asin(payload)
            allowed_asins = {extract_asin_from_url(canonical_url)}
            current_url = _clean_text(getattr(page, "url", ""))
            if current_url:
                try:
                    allowed_asins.add(extract_asin_from_url(current_url))
                except AmazonProductExtractionError:
                    pass
            if payload_asin not in allowed_asins:
                return

            body_size = len(body)
            while observation_sizes and (
                len(observations) >= _MAX_NETWORK_RESPONSE_COUNT
                or total_bytes + body_size > _MAX_NETWORK_TOTAL_BYTES
            ):
                observations.pop(0)
                total_bytes -= observation_sizes.pop(0)
            if (
                len(observations) >= _MAX_NETWORK_RESPONSE_COUNT
                or total_bytes + body_size > _MAX_NETWORK_TOTAL_BYTES
            ):
                return
            total_bytes += body_size
            observation_sizes.append(body_size)
            observations.append(
                {
                    "source_path": urlparse(response_url).path or "/",
                    "payload": payload,
                }
            )
        except (TypeError, ValueError, UnicodeError):
            return
        except Exception:
            return

    try:
        on("response", observe)
    except Exception:
        return lambda: None

    def stop() -> None:
        for method_name in ("remove_listener", "off"):
            method = getattr(page, method_name, None)
            if not callable(method):
                continue
            try:
                method("response", observe)
            except Exception:
                continue
            return

    return stop


def _is_exact_same_origin(candidate: str, expected: str) -> bool:
    def origin(value: str) -> tuple[str, str, int | None]:
        parsed = urlparse(value)
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme.lower() == "https" else 80
        return parsed.scheme.lower(), (parsed.hostname or "").lower(), port

    candidate_origin = origin(candidate)
    return bool(candidate_origin[1]) and candidate_origin == origin(expected)


def _response_headers(response: Any) -> dict[str, Any]:
    value = getattr(response, "headers", {})
    if callable(value):
        value = value()
    if not isinstance(value, Mapping):
        all_headers = getattr(response, "all_headers", None)
        value = all_headers() if callable(all_headers) else {}
    return {str(key).lower(): item for key, item in value.items()}


def _response_content_length(headers: Mapping[str, Any]) -> int:
    raw = _clean_text(headers.get("content-length"))
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return _MAX_NETWORK_RESPONSE_BYTES + 1


def _response_body(response: Any) -> bytes:
    body = getattr(response, "body", None)
    if not callable(body):
        return b""
    value = body()
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    return b""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not allowed: {value}")


def _navigate(page: Any, url: str, *, timeout_ms: int) -> Any:
    navigate = getattr(page, "navigate", None)
    if callable(navigate):
        try:
            return navigate(url, wait_until="domcontentloaded", timeout_ms=timeout_ms)
        except TypeError:
            return navigate(url)

    goto = getattr(page, "goto", None)
    if not callable(goto):
        raise RuntimeError("Browser page does not support navigation.")
    try:
        return goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except TypeError:
        return goto(url)


def _navigation_response_error(response: Any) -> dict[str, Any]:
    if isinstance(response, Mapping):
        raw_status = response.get("status")
    else:
        raw_status = getattr(response, "status", None)
        if callable(raw_status):
            raw_status = raw_status()
    if isinstance(raw_status, bool):
        return {}
    try:
        status = int(raw_status)
    except (TypeError, ValueError):
        return {}
    if status == 429:
        return {
            "error_code": "rate_limited",
            "message": "Amazon navigation returned HTTP 429.",
            "retryable": True,
            "collection_status": "failed",
        }
    if 500 <= status <= 599:
        return {
            "error_code": "transient_page_failure",
            "message": f"Amazon navigation returned HTTP {status}.",
            "retryable": True,
            "collection_status": "failed",
        }
    return {}


def _settle_network_responses(page: Any) -> None:
    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    if not callable(wait_for_timeout):
        return
    try:
        wait_for_timeout(_NETWORK_SETTLE_MS)
    except Exception:
        pass


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
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return b""
    try:
        evaluate(
            """
            document.querySelectorAll(
              '#nav-link-accountList, #nav-global-location-slot, '
              + '#glow-ingress-block, [data-nav-role="signin"], '
              + '[id*="address" i], [class*="address" i], '
              + '[id*="account" i], [class*="account" i], '
              + '[id*="location" i], [class*="location" i]'
            ).forEach((node) => { node.style.visibility = 'hidden'; });
            """
        )
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
    network_data: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    base_key = _raw_capture_base_key(
        artifact_policy,
        requested_asin=requested_asin,
        run_id=run_id,
        observed_at=observed_at,
    )
    refs: list[dict[str, Any]] = []
    try:
        normalized_ref = _upload_bytes(
            context=context,
            artifact_policy=artifact_policy,
            object_key=f"{base_key}/normalized.json",
            payload=capture_bytes,
            capture_kind="normalized_capture",
            content_type="application/json",
            sanitization_status="normalized",
            run_id=run_id,
            observed_at=observed_at,
        )
        refs.append(normalized_ref)
        html_ref = _upload_bytes(
            context=context,
            artifact_policy=artifact_policy,
            object_key=f"{base_key}/page.html.gz",
            payload=gzip.compress(sanitized_html.encode("utf-8"), mtime=0),
            capture_kind="html",
            content_type="application/gzip",
            sanitization_status="sanitized",
            run_id=run_id,
            observed_at=observed_at,
            content_encoding="gzip",
        )
        refs.append(html_ref)
        network_ref = None
        if network_data:
            network_ref = _upload_bytes(
                context=context,
                artifact_policy=artifact_policy,
                object_key=f"{base_key}/page-data.json",
                payload=json.dumps(
                    network_data,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8"),
                capture_kind="network_data",
                content_type="application/json",
                sanitization_status="allowlisted",
                run_id=run_id,
                observed_at=observed_at,
            )
            refs.append(network_ref)
    except Exception as exc:
        raise _ArtifactWriteError(str(exc), artifact_refs=refs) from exc
    return normalized_ref, html_ref, network_ref


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
    try:
        if html:
            refs.append(
                _upload_bytes(
                    context=context,
                    artifact_policy=artifact_policy,
                    object_key=f"{base_key}/page.html.gz",
                    payload=gzip.compress(
                        _sanitize_amazon_html(html).encode("utf-8"),
                        mtime=0,
                    ),
                    capture_kind="html",
                    content_type="application/gzip",
                    sanitization_status="sanitized",
                    run_id=run_id,
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
                    run_id=run_id,
                    observed_at=observed_at,
                )
            )
    except Exception as exc:
        raise _ArtifactWriteError(str(exc), artifact_refs=refs) from exc
    return refs


def _raw_capture_base_key(
    artifact_policy: Mapping[str, Any],
    *,
    requested_asin: str,
    run_id: str,
    observed_at: datetime,
) -> str:
    relative = f"raw-captures/amazon/us/{requested_asin}/{observed_at:%Y/%m/%d}/{run_id}"
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
    run_id: str,
    observed_at: datetime,
    content_encoding: str = "",
) -> dict[str, Any]:
    store = artifact_policy["store"]
    bucket = _required_text(artifact_policy.get("bucket"), "artifact bucket")
    content_digest = hashlib.sha256(payload).hexdigest()
    key_prefix, separator, file_name = object_key.rpartition("/")
    if not separator or not key_prefix or not file_name:
        raise ValueError("Amazon raw capture object_key must include a governed prefix and file.")
    object_key = f"{key_prefix}/{content_digest}/{file_name}"
    with tempfile.TemporaryDirectory(prefix="mujitask-amazon-") as temp_dir:
        local_path = Path(temp_dir) / file_name
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
                "run_id": run_id,
            },
        )
    if stored.bucket != bucket or stored.object_key != object_key:
        raise ValueError("Artifact store returned coordinates outside the requested Amazon key.")
    ref = {
        "capture_kind": capture_kind,
        "bucket": stored.bucket,
        "object_key": stored.object_key,
        "content_digest": content_digest,
        "content_type": content_type,
        "sanitization_status": sanitization_status,
        "request_id": context.request_id,
        "execution_id": context.job_id,
        "run_id": run_id,
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


class _AmazonHTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.script_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.lower()
        if self.skip_depth:
            if normalized_tag not in _VOID_HTML_TAGS:
                self.skip_depth += 1
            return
        if normalized_tag in _DROPPED_HTML_TAGS or _has_sensitive_html_identity(attrs):
            if normalized_tag not in _VOID_HTML_TAGS:
                self.skip_depth = 1
            return
        safe_attrs = _safe_html_attributes(attrs)
        rendered_attrs = "".join(
            f' {name}="{escape(value, quote=True)}"' if value is not None else f" {name}"
            for name, value in safe_attrs
        )
        self.parts.append(f"<{normalized_tag}{rendered_attrs}>")
        if normalized_tag == "script":
            self.script_depth += 1

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if self.skip_depth:
            return
        normalized_tag = tag.lower()
        if normalized_tag in _DROPPED_HTML_TAGS or _has_sensitive_html_identity(attrs):
            return
        safe_attrs = _safe_html_attributes(attrs)
        rendered_attrs = "".join(
            f' {name}="{escape(value, quote=True)}"' if value is not None else f" {name}"
            for name, value in safe_attrs
        )
        self.parts.append(f"<{normalized_tag}{rendered_attrs}/>")

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if self.skip_depth:
            self.skip_depth -= 1
            return
        self.parts.append(f"</{normalized_tag}>")
        if normalized_tag == "script" and self.script_depth:
            self.script_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        self.parts.append(data if self.script_depth else escape(data, quote=False))

    def handle_decl(self, decl: str) -> None:
        if not self.skip_depth and decl.strip().lower() == "doctype html":
            self.parts.append("<!DOCTYPE html>")


def _has_sensitive_html_identity(attrs: list[tuple[str, str | None]]) -> bool:
    for name, value in attrs:
        normalized_name = re.sub(r"[^a-z0-9]", "", str(name).lower())
        normalized_value = re.sub(r"[^a-z0-9]", "", str(value or "").lower())
        if (
            normalized_name.startswith("on")
            or normalized_name in _SENSITIVE_HTML_ATTRIBUTE_NAMES
            or any(marker in normalized_name for marker in _SENSITIVE_HTML_ATTRIBUTE_NAMES)
            or any(marker in normalized_value for marker in _SENSITIVE_HTML_MARKERS)
        ):
            return True
    return False


def _safe_html_attributes(
    attrs: list[tuple[str, str | None]],
) -> list[tuple[str, str | None]]:
    return [
        (name.lower(), value)
        for name, value in attrs
        if name.lower() in _SAFE_HTML_ATTRIBUTES
    ]


def _sanitize_html_structure(html: str) -> str:
    parser = _AmazonHTMLSanitizer()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts)


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
    sanitized = _sanitize_html_structure(sanitized)
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
            if (
                normalized in _SENSITIVE_JSON_KEYS
                or normalized.endswith("token")
                or any(
                    marker in normalized
                    for marker in (
                        "authorization",
                        "cookie",
                        "localstorage",
                        "requestheader",
                    )
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
    statuses = [item.get("status") for item in evidence.values() if isinstance(item, dict)]
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
        source_url = _governed_media_url(_media_url(item))
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


def _normalize_capture_media_urls(capture: dict[str, Any]) -> None:
    media_value = capture.get("media")
    if not isinstance(media_value, Mapping):
        return
    media = dict(media_value)
    raw_main_image = media.get("main_image")
    main_image = _normalize_capture_media_item(raw_main_image)
    gallery_value = media.get("gallery_images")
    gallery_images = []
    if isinstance(gallery_value, list):
        for item in gallery_value:
            normalized_item = _normalize_capture_media_item(item)
            if normalized_item is not None:
                gallery_images.append(normalized_item)
    media["main_image"] = main_image
    media["gallery_images"] = gallery_images
    capture["media"] = media

    evidence_value = capture.get("field_evidence")
    if not isinstance(evidence_value, Mapping):
        return
    evidence = dict(evidence_value)
    for path, raw_value, value in (
        ("media.main_image", raw_main_image, main_image),
        ("media.gallery_images", gallery_value, gallery_images),
    ):
        item = evidence.get(path)
        if not isinstance(item, Mapping):
            continue
        normalized_item = dict(item)
        normalized_item["value"] = value
        rejected = bool(raw_value) and not value
        if isinstance(raw_value, list):
            rejected = len(value) != len(raw_value)
        if rejected and capture.get("collection_status") == "success":
            capture["collection_status"] = "partial_success"
        if bool(raw_value) and not value:
            normalized_item.update(
                {
                    "status": "missing",
                    "source_kind": None,
                    "source_locator": None,
                    "confidence": 0.0,
                }
            )
        evidence[path] = normalized_item
    capture["field_evidence"] = evidence


def _normalize_capture_media_item(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    source_url = _governed_media_url(_clean_text(value.get("url")))
    return {"url": source_url} if source_url else None


def _governed_media_url(value: str) -> str:
    if not value or any(character.isspace() for character in value):
        return ""
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return ""
    hostname = (parsed.hostname or "").lower()
    allowed_host = any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in ("media-amazon.com", "ssl-images-amazon.com")
    )
    if (
        parsed.scheme.lower() != "https"
        or not allowed_host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return ""
    return parsed._replace(
        scheme="https",
        netloc=hostname,
        query="",
        fragment="",
    ).geturl()


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


def _browser_provider_name(value: Any) -> str:
    provider_name = _clean_text(value)
    return provider_name if _BROWSER_PROVIDER_NAME.fullmatch(provider_name) else ""


def _browser_stage_durations(value: Any) -> dict[str, float]:
    raw = value if isinstance(value, Mapping) else {}
    durations: dict[str, float] = {}
    for stage_name in _BROWSER_STAGE_NAMES:
        duration = raw.get(stage_name)
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            continue
        normalized = float(duration)
        if math.isfinite(normalized) and normalized >= 0:
            durations[stage_name] = round(normalized, 3)
    return durations


def _elapsed_ms(started_at: float) -> float:
    return round(max(perf_counter() - started_at, 0.0) * 1_000.0, 3)


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
    browser_provider_name: str = "",
    stage_durations_ms: Mapping[str, Any] | None = None,
) -> HandlerResult:
    refs = list(artifact_refs or [])
    provider_name = _browser_provider_name(browser_provider_name)
    stage_durations = _browser_stage_durations(stage_durations_ms)
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
    if provider_name:
        result["browser_provider_name"] = provider_name
    if stage_durations:
        result["stage_durations_ms"] = stage_durations
    summary = {
        "marketplace_code": "US",
        "requested_asin": requested_asin,
        "collection_status": collection_status,
        "artifact_count": len(refs),
        "browser_target_digest": browser_target_digest,
        "error_code": error_code,
    }
    if provider_name:
        summary["browser_provider_name"] = provider_name
    if stage_durations:
        summary["stage_durations_ms"] = stage_durations
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
        summary=summary,
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
