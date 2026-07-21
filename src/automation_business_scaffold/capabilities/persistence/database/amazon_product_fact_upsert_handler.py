from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Mapping

from automation_business_scaffold.capabilities.browser.amazon.product_page import (
    AmazonProductExtractionError,
    extract_amazon_network_product_data,
    normalize_amazon_media_url,
    normalize_asin,
)
from automation_business_scaffold.config import get_execution_control_defaults
from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    coerce_mapping,
    coerce_mapping_list,
    failed_result,
    first_non_empty,
    partial_success_result,
    success_result,
)
from automation_business_scaffold.infrastructure.artifacts.artifact_store import (
    create_artifact_store,
    join_object_key,
    normalize_artifact_store_provider,
)
from automation_business_scaffold.infrastructure.facts.amazon_fact_store import (
    AmazonFactCollisionError,
    AmazonFactSchemaUnavailableError,
    AmazonFactSchemaVersionError,
    AmazonFactStore,
)
from automation_business_scaffold.infrastructure.schemas.amazon_fact_schema import (
    AMAZON_FACT_SCHEMA_REVISION,
)


HANDLER_CODE = "amazon_product_fact_upsert"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]
_COLLECTION_STATUSES = {"success", "partial_success", "unavailable"}
_EVIDENCE_STATUSES = {"observed", "missing", "explicitly_unavailable"}
_AVAILABILITY_STATUSES = {"in_stock", "out_of_stock", "unavailable", "unknown"}
_FULFILLMENT_CHANNELS = {"amazon", "merchant", "unknown"}
_PROMOTION_TYPES = {
    "coupon",
    "limited_time_deal",
}
_PROMOTION_DISCOUNT_TYPES = {"percentage", "amount", "price_override"}
_PROMOTION_KEYS = {
    "promotion_type",
    "label",
    "discount_type",
    "discount_value",
    "deal_price",
    "reference_price",
    "reference_price_type",
    "currency",
    "prime_only",
    "claim_required",
    "raw_text",
}
_PROMOTION_SENSITIVE_PATTERN = re.compile(
    r"(?:anti-csrf|offerlistingid|promotionid|window\.location|document\.cookie|"
    r"authorization|bearer\s|(?:access[_-]?token|token|cookie|password|credential)\s*[=:])",
    re.IGNORECASE,
)
_BOUGHT_PAST_MONTH_VALUE_PATTERN = re.compile(
    r"^[0-9][0-9,]*(?:\.[0-9]+)?[KkMm]?\+?$"
)
_MATERIALIZED_MEDIA_STATES = {"uploaded", "reused", "reused_in_run"}
_MATERIALIZED_MEDIA_ROLES = {"main_image", "gallery_image"}
_MATERIALIZED_MEDIA_MIME_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
_MAX_MATERIALIZED_MEDIA_BYTES = 25 * 1024 * 1024
_MAX_NORMALIZED_CAPTURE_BYTES = 2 * 1024 * 1024
_MAX_SANITIZED_HTML_BYTES = 8 * 1024 * 1024
_RAW_CAPTURE_MAX_BYTES = {
    "normalized_capture": _MAX_NORMALIZED_CAPTURE_BYTES,
    "html": 2 * 1024 * 1024,
    "network_data": 512 * 1024,
    "screenshot": 10 * 1024 * 1024,
}
_HTML_SCRIPT_PATTERN = re.compile(
    r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_SENSITIVE_ATTRIBUTE_PATTERN = re.compile(
    r"\s+(?:data-)?(?:cookie|authorization|auth-token|access-token|refresh-token|token|"
    r"localstorage|workspace|profile|account|address|request-headers?)\s*=",
    re.IGNORECASE,
)
_HTML_SENSITIVE_MARKERS = (
    "accountname",
    "addressbook",
    "customername",
    "deliveryaddress",
    "glowingress",
    "navgloballocation",
    "navlinkaccountlist",
    "shippingaddress",
)
_HTML_FORBIDDEN_TAGS = {
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
_HTML_SENSITIVE_JSON_KEYS = {
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
_RAW_CAPTURE_POLICIES = {
    "normalized_capture": ("application/json", "normalized"),
    "html": ("application/gzip", "sanitized"),
    "network_data": ("application/json", "allowlisted"),
    "screenshot": ("image/png", "not_applicable"),
}
_LEGACY_FIELD_EVIDENCE_PATHS = frozenset(
    {
        "product.title",
        "product.brand",
        "product.category_path",
        "product.bullet_points",
        "product.description",
        "product.technical_details",
        "commerce.availability_status",
        "commerce.rating",
        "commerce.review_count",
        "commerce.featured_offer.seller_id",
        "commerce.featured_offer.seller_name",
        "commerce.featured_offer.is_buy_box",
        "commerce.featured_offer.price_amount",
        "commerce.featured_offer.list_price_amount",
        "commerce.featured_offer.currency",
        "commerce.featured_offer.fulfillment_channel",
        "commerce.featured_offer.delivery_text",
        "commerce.featured_offer.coupon_text",
        "commerce.featured_offer.promotions",
        "variants.parent_asin",
        "variants.child_asins",
        "variants.current_attributes",
        "variants.dimensions",
        "rankings",
        "media.main_image",
        "media.gallery_images",
    }
)
_REQUIRED_FIELD_EVIDENCE_PATHS = _LEGACY_FIELD_EVIDENCE_PATHS | {
    "commerce.bought_past_month",
}
_COLLECTION_STATUS_OPTIONAL_EVIDENCE_PATHS = {
    "commerce.bought_past_month",
}


class _InvalidCapture(ValueError):
    pass


def amazon_product_fact_upsert_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    fact_store = _resolve_fact_store(context, payload)
    if fact_store is None:
        return _configuration_failure(
            context,
            error_code="fact_database_persistence_required",
            message=(
                "amazon_product_fact_upsert requires Fact DB persistence, but no Fact DB "
                "configuration was provided."
            ),
        )

    owns_fact_store = context.metadata.get("fact_store") is None
    try:
        return _amazon_product_fact_upsert_with_store(
            context,
            payload=payload,
            fact_store=fact_store,
        )
    finally:
        if owns_fact_store:
            close = getattr(fact_store, "close", None)
            if callable(close):
                close()


def _amazon_product_fact_upsert_with_store(
    context: HandlerContext,
    *,
    payload: dict[str, Any],
    fact_store: Any,
) -> HandlerResult:
    require_schema_revision = getattr(fact_store, "require_schema_revision", None)
    if callable(require_schema_revision):
        try:
            require_schema_revision()
        except AmazonFactSchemaUnavailableError as exc:
            return failed_result(
                context,
                error=build_error(
                    error_type="infrastructure",
                    error_code="amazon_fact_schema_check_failed",
                    message=str(exc),
                    retryable=True,
                    details={
                        "required_fact_schema_revision": AMAZON_FACT_SCHEMA_REVISION,
                    },
                ),
                summary={"persistence_mode": "schema_check_failed"},
                result={
                    "required_fact_schema_revision": AMAZON_FACT_SCHEMA_REVISION,
                },
            )
        except AmazonFactSchemaVersionError as exc:
            return failed_result(
                context,
                error=build_error(
                    error_type="infrastructure",
                    error_code="amazon_fact_schema_not_ready",
                    message=str(exc),
                    retryable=False,
                    details={
                        "required_fact_schema_revision": AMAZON_FACT_SCHEMA_REVISION,
                    },
                ),
                summary={"persistence_mode": "schema_not_ready"},
                result={
                    "required_fact_schema_revision": AMAZON_FACT_SCHEMA_REVISION,
                },
            )

    artifact_store = _resolve_artifact_store(context, payload)
    if artifact_store is None:
        return _configuration_failure(
            context,
            error_code="object_storage_required",
            message=(
                "amazon_product_fact_upsert requires non-local object storage with read access."
            ),
        )
    artifact_policy = _resolve_artifact_policy(context, payload, artifact_store)
    if artifact_policy is None:
        return _configuration_failure(
            context,
            error_code="object_storage_required",
            message="amazon_product_fact_upsert requires a governed artifact bucket.",
        )

    try:
        normalized_ref, raw_capture_refs = _validate_raw_capture_evidence(
            payload,
            artifact_policy,
        )
    except ValueError as exc:
        return _validation_failure(
            context,
            error_code="raw_capture_evidence_missing",
            message=str(exc),
        )

    try:
        capture_bytes = _read_artifact_bytes(
            artifact_store,
            bucket=normalized_ref["bucket"],
            object_key=normalized_ref["object_key"],
            max_bytes=_MAX_NORMALIZED_CAPTURE_BYTES,
        )
        _verify_content_digest(capture_bytes, normalized_ref)
    except ValueError as exc:
        return _validation_failure(
            context,
            error_code="capture_artifact_digest_mismatch",
            message=str(exc),
        )
    except Exception as exc:
        return failed_result(
            context,
            error=build_error(
                error_type="artifact_read_failure",
                error_code="normalized_capture_read_failed",
                message=str(exc),
                retryable=True,
                details={
                    "bucket": normalized_ref["bucket"],
                    "object_key": normalized_ref["object_key"],
                },
            ),
            summary={"persistence_mode": "object_storage_read_failed"},
        )

    try:
        capture = _decode_and_validate_capture(capture_bytes)
        expected_requested_asin = normalize_asin(
            _required_text(payload.get("requested_asin"), "requested_asin")
        )
        if capture["requested_asin"] != expected_requested_asin:
            raise _InvalidCapture(
                "Normalized capture requested_asin does not match the source-row ASIN."
            )
        source_table_ref = _validate_source_binding(payload)
        run_id = _required_text(payload.get("run_id"), "run_id")
        source_record_id = _required_text(payload.get("source_record_id"), "source_record_id")
        _validate_raw_capture_identity(
            raw_capture_refs,
            capture,
            artifact_policy,
            request_id=context.request_id,
            run_id=run_id,
        )
        materialized_media_assets, media_coverage = _validate_materialized_media_assets(
            payload,
            capture,
            artifact_policy,
        )
        payload["materialized_media_assets"] = materialized_media_assets
    except (
        AmazonProductExtractionError,
        RecursionError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        return _validation_failure(
            context,
            error_code="invalid_amazon_capture",
            message=str(exc),
        )

    try:
        _verify_raw_capture_objects(
            artifact_store,
            raw_capture_refs,
            capture=capture,
            normalized_ref=normalized_ref,
            normalized_bytes=capture_bytes,
        )
    except ValueError as exc:
        return _validation_failure(
            context,
            error_code="raw_capture_digest_mismatch",
            message=str(exc),
        )
    except Exception as exc:
        return failed_result(
            context,
            error=build_error(
                error_type="artifact_read_failure",
                error_code="raw_capture_read_failed",
                message=str(exc),
                retryable=True,
            ),
            summary={"persistence_mode": "object_storage_read_failed"},
        )

    try:
        _verify_materialized_media_objects(
            artifact_store,
            materialized_media_assets,
        )
    except ValueError as exc:
        return _validation_failure(
            context,
            error_code="materialized_media_digest_mismatch",
            message=str(exc),
        )
    except Exception as exc:
        return failed_result(
            context,
            error=build_error(
                error_type="artifact_read_failure",
                error_code="materialized_media_read_failed",
                message=str(exc),
                retryable=True,
            ),
            summary={"persistence_mode": "object_storage_read_failed"},
        )

    try:
        transaction = getattr(fact_store, "transaction", None)
        if not callable(transaction):
            raise RuntimeError("Amazon Fact store must provide transactional bundle writes.")
        with transaction() as transaction_store:
            persisted = _persist_capture(
                context=context,
                payload=payload,
                capture=capture,
                capture_bytes=capture_bytes,
                normalized_ref=normalized_ref,
                raw_capture_refs=raw_capture_refs,
                source_table_ref=source_table_ref,
                source_record_id=source_record_id,
                run_id=run_id,
                fact_store=transaction_store,
            )
    except AmazonFactCollisionError as exc:
        return _validation_failure(
            context,
            error_code=exc.error_code,
            message=str(exc),
        )
    except Exception as exc:  # pragma: no cover - defensive worker boundary
        return failed_result(
            context,
            error=build_error(
                error_type="persistence_failure",
                error_code="amazon_product_fact_upsert_failed",
                message=str(exc),
                retryable=True,
                details={
                    "marketplace_code": capture["marketplace_code"],
                    "requested_asin": capture["requested_asin"],
                    "run_id": run_id,
                },
            ),
            summary={
                "collection_status": capture["collection_status"],
                "persistence_mode": "failed",
            },
        )

    collection_status = _effective_collection_status(
        capture["collection_status"],
        media_coverage,
    )
    projection_facts = persisted["projection_facts"]
    projection_facts["collection_status"] = collection_status
    _suppress_incomplete_media_projection(
        projection_facts,
        capture=capture,
        materialized_media_assets=materialized_media_assets,
    )

    if context.metadata.get("include_transient_projection_facts") is not True:
        persisted.pop("projection_facts", None)

    summary = {
        "collection_status": collection_status,
        "marketplace_code": capture["marketplace_code"],
        "requested_asin": capture["requested_asin"],
        "persistence_mode": "database",
        "persisted_counts": dict(persisted["persisted_counts"]),
        "media_coverage": dict(media_coverage),
    }
    if collection_status == "partial_success":
        warnings: list[str] = []
        if capture["collection_status"] == "partial_success":
            warnings.append("Amazon capture contains fields without observed evidence.")
        if not media_coverage["complete"]:
            warnings.append("Amazon media materialization is incomplete.")
        return partial_success_result(
            context,
            summary=summary,
            result=persisted,
            warnings=tuple(warnings),
        )
    return success_result(context, summary=summary, result=persisted)


def _resolve_fact_store(context: HandlerContext, payload: dict[str, Any]) -> Any | None:
    injected = context.metadata.get("fact_store")
    if injected is not None:
        return injected

    request_payload = coerce_mapping(payload.get("request_payload"))
    fact_db_url = first_non_empty(
        payload.get("fact_db_url"),
        payload.get("execution_control_fact_db_url"),
        request_payload.get("fact_db_url"),
        request_payload.get("execution_control_fact_db_url"),
        coerce_mapping(payload.get("persistence")).get("fact_db_url"),
        coerce_mapping(request_payload.get("persistence")).get("fact_db_url"),
        get_execution_control_defaults().fact_db_url,
    )
    if not fact_db_url:
        return None
    return AmazonFactStore(db_url=fact_db_url)


def _resolve_artifact_store(context: HandlerContext, payload: dict[str, Any]) -> Any | None:
    injected = context.metadata.get("artifact_store")
    if injected is not None:
        return injected if callable(getattr(injected, "read_bytes", None)) else None

    settings = _artifact_settings(context, payload)
    provider = normalize_artifact_store_provider(settings.get("artifact_store_provider"))
    if provider == "local" or not _clean_text(settings.get("artifact_bucket")):
        return None
    try:
        store = create_artifact_store(settings)
    except (RuntimeError, ValueError):
        return None
    if store is None or not callable(getattr(store, "read_bytes", None)):
        return None
    return store


def _artifact_settings(context: HandlerContext, payload: dict[str, Any]) -> dict[str, Any]:
    request_payload = coerce_mapping(payload.get("request_payload"))
    defaults = get_execution_control_defaults()
    settings: dict[str, Any] = {
        "artifact_store_provider": defaults.artifact_store_provider,
        "artifact_bucket": defaults.artifact_bucket,
        "artifact_object_prefix": defaults.artifact_object_prefix,
        "artifact_root": defaults.artifact_root,
        "minio_endpoint": defaults.minio_endpoint,
        "minio_access_key": defaults.minio_access_key,
        "minio_secret_key": defaults.minio_secret_key,
        "minio_secure": defaults.minio_secure,
        "minio_region": defaults.minio_region,
        "minio_create_bucket": defaults.minio_create_bucket,
    }
    if context.metadata.get("allow_test_persistence_overrides") is not True:
        return settings
    for source in (
        coerce_mapping(request_payload.get("artifact_store")),
        coerce_mapping(payload.get("artifact_store")),
    ):
        for source_key, target_key in (
            ("provider", "artifact_store_provider"),
            ("artifact_store_provider", "artifact_store_provider"),
            ("bucket", "artifact_bucket"),
            ("artifact_bucket", "artifact_bucket"),
            ("object_prefix", "artifact_object_prefix"),
            ("artifact_object_prefix", "artifact_object_prefix"),
            ("minio_endpoint", "minio_endpoint"),
            ("minio_access_key", "minio_access_key"),
            ("minio_secret_key", "minio_secret_key"),
            ("minio_secure", "minio_secure"),
            ("minio_region", "minio_region"),
            ("minio_create_bucket", "minio_create_bucket"),
        ):
            if source.get(source_key) not in (None, ""):
                settings[target_key] = source[source_key]

    for target_key, aliases in {
        "artifact_store_provider": (
            "artifact_store_provider",
            "execution_control_artifact_store_provider",
        ),
        "artifact_bucket": ("artifact_bucket", "execution_control_artifact_bucket"),
        "artifact_object_prefix": (
            "artifact_object_prefix",
            "execution_control_artifact_object_prefix",
        ),
        "minio_endpoint": ("minio_endpoint", "execution_control_minio_endpoint"),
        "minio_access_key": ("minio_access_key", "execution_control_minio_access_key"),
        "minio_secret_key": ("minio_secret_key", "execution_control_minio_secret_key"),
        "minio_secure": ("minio_secure", "execution_control_minio_secure"),
        "minio_region": ("minio_region", "execution_control_minio_region"),
        "minio_create_bucket": (
            "minio_create_bucket",
            "execution_control_minio_create_bucket",
        ),
    }.items():
        value = _first_value(payload, request_payload, aliases=aliases)
        if value not in (None, ""):
            settings[target_key] = value
    return settings


def _resolve_artifact_policy(
    context: HandlerContext,
    payload: dict[str, Any],
    artifact_store: Any,
) -> dict[str, str] | None:
    settings = _artifact_settings(context, payload)
    bucket = _artifact_policy_value(
        context,
        artifact_store,
        settings,
        metadata_key="artifact_bucket",
        store_attribute="artifact_bucket",
    )
    if not bucket:
        return None
    object_prefix = _artifact_policy_value(
        context,
        artifact_store,
        settings,
        metadata_key="artifact_object_prefix",
        store_attribute="artifact_object_prefix",
    )
    return {"bucket": bucket, "object_prefix": object_prefix.strip("/")}


def _artifact_policy_value(
    context: HandlerContext,
    artifact_store: Any,
    settings: Mapping[str, Any],
    *,
    metadata_key: str,
    store_attribute: str,
) -> str:
    if metadata_key in context.metadata:
        return _clean_text(context.metadata.get(metadata_key))
    if hasattr(artifact_store, store_attribute):
        return _clean_text(getattr(artifact_store, store_attribute))
    return _clean_text(settings.get(metadata_key))


def _first_value(
    payload: Mapping[str, Any],
    request_payload: Mapping[str, Any],
    *,
    aliases: tuple[str, ...],
) -> Any:
    for source in (payload, request_payload):
        for key in aliases:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def _validate_raw_capture_evidence(
    payload: dict[str, Any],
    artifact_policy: Mapping[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized_ref = coerce_mapping(payload.get("normalized_capture_ref"))
    raw_refs = coerce_mapping_list(payload.get("raw_capture_refs"))
    if not normalized_ref:
        raise ValueError("normalized_capture_ref is required.")

    normalized_raw: dict[str, Any] | None = None
    html_raw: dict[str, Any] | None = None
    validated_refs: list[dict[str, Any]] = []
    seen_kinds: set[str] = set()
    for raw_ref in raw_refs:
        capture_kind = _required_text(raw_ref.get("capture_kind"), "capture_kind")
        policy = _RAW_CAPTURE_POLICIES.get(capture_kind)
        if policy is None:
            raise ValueError(f"Raw capture kind is not allowed: {capture_kind}.")
        if capture_kind in seen_kinds:
            raise ValueError(f"Raw capture kind must be unique: {capture_kind}.")
        seen_kinds.add(capture_kind)
        validated = _validate_artifact_ref(
            raw_ref,
            expected_kind=capture_kind,
            expected_content_type=policy[0],
            expected_sanitization_status=policy[1],
        )
        _validate_governed_artifact_ref(
            validated,
            artifact_policy=artifact_policy,
            relative_prefix="raw-captures/amazon/us",
        )
        validated_refs.append(validated)
        if capture_kind == "normalized_capture":
            normalized_raw = validated
        elif capture_kind == "html":
            html_raw = validated

    if normalized_raw is None or html_raw is None:
        raise ValueError(
            "raw_capture_refs must contain normalized_capture and sanitized html evidence."
        )
    normalized_policy = _RAW_CAPTURE_POLICIES["normalized_capture"]
    normalized = _validate_artifact_ref(
        normalized_ref,
        expected_kind="normalized_capture",
        expected_content_type=normalized_policy[0],
        expected_sanitization_status=normalized_policy[1],
    )
    _validate_governed_artifact_ref(
        normalized,
        artifact_policy=artifact_policy,
        relative_prefix="raw-captures/amazon/us",
    )
    normalized_raw = _validate_artifact_ref(
        normalized_raw,
        expected_kind="normalized_capture",
        expected_content_type=normalized_policy[0],
        expected_sanitization_status=normalized_policy[1],
    )
    identity_fields = (
        "bucket",
        "object_key",
        "content_digest",
        "content_type",
        "sanitization_status",
        "request_id",
        "execution_id",
        "run_id",
    )
    if any(normalized[field] != normalized_raw[field] for field in identity_fields):
        raise ValueError(
            "normalized_capture_ref must exactly match the normalized_capture raw evidence."
        )
    return normalized, validated_refs


def _validate_raw_capture_identity(
    raw_capture_refs: list[dict[str, Any]],
    capture: Mapping[str, Any],
    artifact_policy: Mapping[str, str],
    *,
    request_id: str,
    run_id: str,
) -> None:
    captured_at = datetime.fromtimestamp(capture["captured_at_epoch"], tz=timezone.utc)
    relative_prefix = (
        f"raw-captures/amazon/us/{capture['requested_asin']}/{captured_at:%Y/%m/%d}/{run_id}"
    )
    expected_filenames = {
        "normalized_capture": "normalized.json",
        "html": "page.html.gz",
        "network_data": "page-data.json",
        "screenshot": "page.png",
    }
    normalized_ref = next(
        ref for ref in raw_capture_refs if ref["capture_kind"] == "normalized_capture"
    )
    origin_execution_id = normalized_ref["execution_id"]
    for raw_ref in raw_capture_refs:
        if raw_ref["request_id"] != request_id:
            raise ValueError("Raw capture request_id must match the active Runtime request.")
        if raw_ref["execution_id"] != origin_execution_id:
            raise ValueError("Raw capture refs must share one browser execution_id.")
        if raw_ref["run_id"] != run_id:
            raise ValueError("Raw capture run_id must match the persist job run_id.")
        _validate_governed_artifact_ref(
            raw_ref,
            artifact_policy=artifact_policy,
            relative_prefix=relative_prefix,
        )
        expected_key = join_object_key(
            artifact_policy.get("object_prefix", ""),
            (
                f"{relative_prefix}/{raw_ref['content_digest']}/"
                f"{expected_filenames[raw_ref['capture_kind']]}"
            ),
        )
        if raw_ref["object_key"] != expected_key:
            raise ValueError(
                "Raw capture object_key must match its ASIN, capture date, run_id, "
                "content digest, and governed filename."
            )


def _verify_raw_capture_objects(
    artifact_store: Any,
    raw_capture_refs: list[dict[str, Any]],
    *,
    capture: Mapping[str, Any],
    normalized_ref: Mapping[str, Any],
    normalized_bytes: bytes,
) -> None:
    normalized_coordinate = (
        normalized_ref["bucket"],
        normalized_ref["object_key"],
    )
    for raw_ref in raw_capture_refs:
        coordinate = (raw_ref["bucket"], raw_ref["object_key"])
        if coordinate == normalized_coordinate:
            payload = normalized_bytes
        else:
            payload = _read_artifact_bytes(
                artifact_store,
                bucket=raw_ref["bucket"],
                object_key=raw_ref["object_key"],
                max_bytes=_RAW_CAPTURE_MAX_BYTES[raw_ref["capture_kind"]],
            )
        if not isinstance(payload, (bytes, bytearray)):
            raise ValueError("Raw capture object storage must return bytes.")
        actual_digest = hashlib.sha256(bytes(payload)).hexdigest()
        if actual_digest != raw_ref["content_digest"]:
            raise ValueError(
                f"{raw_ref['capture_kind']} raw capture digest does not match stored bytes."
            )
        if raw_ref["capture_kind"] == "network_data":
            _validate_network_capture(bytes(payload), capture)
        elif raw_ref["capture_kind"] == "html":
            _validate_html_capture(bytes(payload))


class _SanitizedHTMLPolicyValidator(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.violation = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in _HTML_FORBIDDEN_TAGS:
            self.violation = True
            return
        for name, value in attrs:
            normalized_name = re.sub(r"[^a-z0-9]", "", str(name).lower())
            normalized_value = re.sub(r"[^a-z0-9]", "", str(value or "").lower())
            if (
                normalized_name.startswith("on")
                or any(marker in normalized_name for marker in _HTML_SENSITIVE_JSON_KEYS)
                or any(marker in normalized_value for marker in _HTML_SENSITIVE_MARKERS)
            ):
                self.violation = True

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_comment(self, data: str) -> None:
        del data
        self.violation = True


def _html_violates_sanitization_policy(html: str) -> bool:
    validator = _SanitizedHTMLPolicyValidator()
    validator.feed(html)
    validator.close()
    return validator.violation


def _validate_html_capture(payload: bytes) -> None:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as compressed:
            decompressed = compressed.read(_MAX_SANITIZED_HTML_BYTES + 1)
        if len(decompressed) > _MAX_SANITIZED_HTML_BYTES:
            raise ValueError("html raw capture exceeds the decompressed size limit.")
        html = decompressed.decode("utf-8")
    except (EOFError, OSError, UnicodeDecodeError) as exc:
        raise ValueError("html raw capture must contain valid gzip-compressed UTF-8.") from exc
    if not html.strip():
        raise ValueError("html raw capture must contain sanitized page evidence.")
    if _html_violates_sanitization_policy(html):
        raise ValueError("html raw capture contains content removed by sanitization policy.")
    if _HTML_SENSITIVE_ATTRIBUTE_PATTERN.search(html):
        raise ValueError("html raw capture contains sensitive attributes.")
    if re.search(
        r"<meta\b[^>]*(?:http-equiv|authorization|cookie|token)[^>]*>",
        html,
        flags=re.IGNORECASE,
    ):
        raise ValueError("html raw capture contains sensitive metadata.")
    if re.search(
        r"\bBearer\s+(?!\[REDACTED\])[^\s<]+",
        html,
        flags=re.IGNORECASE,
    ):
        raise ValueError("html raw capture contains an unredacted bearer credential.")
    script_matches = list(_HTML_SCRIPT_PATTERN.finditer(html))
    for match in script_matches:
        attrs = match.group("attrs").strip().lower()
        if attrs not in {
            'type="application/ld+json"',
            'id="amazon-product-state" type="application/json"',
        }:
            raise ValueError("html raw capture contains a non-allowlisted script.")
        try:
            script_value = json.loads(match.group("body"))
        except (TypeError, ValueError) as exc:
            raise ValueError("html raw capture contains invalid allowlisted JSON.") from exc
        if _contains_sensitive_json_key(script_value):
            raise ValueError("html raw capture contains sensitive structured-data fields.")
    if re.search(
        r"</?script\b",
        _HTML_SCRIPT_PATTERN.sub("", html),
        flags=re.IGNORECASE,
    ):
        raise ValueError("html raw capture contains an incomplete script element.")


def _contains_sensitive_json_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if (
                normalized in _HTML_SENSITIVE_JSON_KEYS
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
                return True
            if _contains_sensitive_json_key(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_sensitive_json_key(item) for item in value)
    return False


def _validate_network_capture(payload: bytes, capture: Mapping[str, Any]) -> None:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("network_data raw capture must contain valid UTF-8 JSON.") from exc
    if not isinstance(decoded, dict):
        raise ValueError("network_data raw capture must contain a JSON object.")

    source_locator = _required_text(
        decoded.get("source_locator"),
        "network_data source_locator",
    )
    source_path, separator, _ = source_locator.partition("#sha256=")
    if not separator or not source_path.startswith("/"):
        raise ValueError("network_data source_locator must use the governed digest form.")
    canonical = extract_amazon_network_product_data(
        [{"source_path": source_path, "payload": decoded}],
        expected_asin=capture["resolved_asin"],
    )
    if canonical != decoded:
        raise ValueError(
            "network_data raw capture must contain only canonical allowlisted fields "
            "for the resolved ASIN."
        )


def _validate_governed_artifact_ref(
    artifact_ref: Mapping[str, Any],
    *,
    artifact_policy: Mapping[str, str],
    relative_prefix: str,
) -> None:
    expected_bucket = artifact_policy["bucket"]
    if artifact_ref.get("bucket") != expected_bucket:
        raise ValueError("Artifact evidence must use the configured artifact bucket.")
    governed_prefix = join_object_key(
        artifact_policy.get("object_prefix", ""),
        relative_prefix,
    ).rstrip("/")
    object_key = _clean_text(artifact_ref.get("object_key"))
    if not object_key.startswith(f"{governed_prefix}/"):
        raise ValueError("Artifact evidence object_key is outside its governed Amazon prefix.")


def _validate_artifact_ref(
    value: Mapping[str, Any],
    *,
    expected_kind: str,
    expected_content_type: str,
    expected_sanitization_status: str,
) -> dict[str, Any]:
    ref = {
        key: value[key]
        for key in (
            "capture_kind",
            "bucket",
            "object_key",
            "content_digest",
            "content_type",
            "sanitization_status",
            "request_id",
            "execution_id",
            "run_id",
            "collected_at",
            "created_at",
        )
        if key in value
    }
    if _clean_text(ref.get("capture_kind")) != expected_kind:
        raise ValueError(f"Artifact evidence must use capture_kind={expected_kind}.")
    ref["bucket"] = _required_text(ref.get("bucket"), "artifact bucket")
    ref["object_key"] = _required_text(ref.get("object_key"), "artifact object_key")
    ref["content_digest"] = _required_sha256_digest(
        ref.get("content_digest"),
        "artifact content_digest",
    )
    object_key_parts = ref["object_key"].split("/")
    if len(object_key_parts) < 2 or object_key_parts[-2] != ref["content_digest"]:
        raise ValueError(f"{expected_kind} raw capture object_key must contain its content digest.")
    ref["content_type"] = _required_text(ref.get("content_type"), "artifact content_type")
    if ref["content_type"] != expected_content_type:
        raise ValueError(
            f"{expected_kind} raw capture must use content_type={expected_content_type}."
        )
    ref["sanitization_status"] = _required_text(
        ref.get("sanitization_status"),
        "artifact sanitization_status",
    )
    if ref["sanitization_status"] != expected_sanitization_status:
        raise ValueError(
            f"{expected_kind} raw capture must use "
            f"sanitization_status={expected_sanitization_status}."
        )
    ref["request_id"] = _required_text(ref.get("request_id"), "artifact request_id")
    ref["execution_id"] = _required_text(
        ref.get("execution_id"),
        "artifact execution_id",
    )
    ref["run_id"] = _required_text(ref.get("run_id"), "artifact run_id")
    return ref


def _verify_content_digest(capture_bytes: bytes, normalized_ref: Mapping[str, Any]) -> None:
    expected = _clean_text(normalized_ref.get("content_digest")).lower()
    if not expected:
        return
    actual = hashlib.sha256(capture_bytes).hexdigest()
    if actual != expected:
        raise ValueError("Normalized capture content digest does not match the stored bytes.")


def _decode_and_validate_capture(capture_bytes: bytes) -> dict[str, Any]:
    decoded = json.loads(capture_bytes.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise _InvalidCapture("Normalized capture must be a JSON object.")
    capture = dict(decoded)
    contract_revision = capture.get("contract_revision")
    if isinstance(contract_revision, bool) or contract_revision not in {1, 2, 3, 4, 5}:
        raise _InvalidCapture("Normalized capture contract_revision must equal 1, 2, 3, 4, or 5.")
    if capture.get("source_platform") != "amazon":
        raise _InvalidCapture("Normalized capture source_platform must equal amazon.")
    if capture.get("marketplace_code") != "US":
        raise _InvalidCapture("Only marketplace_code=US is supported.")

    requested_asin = normalize_asin(capture.get("requested_asin"))
    resolved_asin = normalize_asin(capture.get("resolved_asin"))
    capture["requested_asin"] = requested_asin
    capture["resolved_asin"] = resolved_asin
    expected_url = f"https://www.amazon.com/dp/{requested_asin}"
    if capture.get("canonical_url") != expected_url:
        raise _InvalidCapture("Normalized capture canonical_url does not match requested_asin.")

    capture["captured_at_epoch"] = _iso_timestamp(capture.get("captured_at"))
    collection_status = _clean_text(capture.get("collection_status"))
    if collection_status not in _COLLECTION_STATUSES:
        raise _InvalidCapture("Normalized capture collection_status is invalid.")

    for key in ("product", "commerce", "variants", "media", "field_evidence"):
        if not isinstance(capture.get(key), dict):
            raise _InvalidCapture(f"Normalized capture {key} must be an object.")
    if not isinstance(capture.get("rankings"), list):
        raise _InvalidCapture("Normalized capture rankings must be an array.")
    if capture.get("profile_context") is not None and not isinstance(
        capture.get("profile_context"), dict
    ):
        raise _InvalidCapture("Normalized capture profile_context must be an object.")
    if contract_revision in {1, 2}:
        _adapt_legacy_capture_media_urls(capture)

    evidence = capture["field_evidence"]
    evidence_paths = set(evidence)
    expected_evidence_paths = (
        _REQUIRED_FIELD_EVIDENCE_PATHS
        if contract_revision >= 5
        else _LEGACY_FIELD_EVIDENCE_PATHS
    )
    if evidence_paths != expected_evidence_paths:
        missing = sorted(expected_evidence_paths - evidence_paths)
        unexpected = sorted(evidence_paths - expected_evidence_paths)
        raise _InvalidCapture(
            "Normalized capture field_evidence must exactly cover its contract revision; "
            f"missing={missing}, unexpected={unexpected}."
        )
    for path, item in evidence.items():
        if not isinstance(path, str) or not isinstance(item, dict):
            raise _InvalidCapture("Every field_evidence entry must be an object keyed by path.")
        _validate_field_evidence_item(capture, path, item)
    missing_evidence = any(
        item["status"] == "missing"
        for path, item in evidence.items()
        if path not in _COLLECTION_STATUS_OPTIONAL_EVIDENCE_PATHS
    )
    if collection_status == "success" and missing_evidence:
        raise _InvalidCapture("collection_status=success cannot contain missing field evidence.")
    _validate_capture_sections(capture, contract_revision=contract_revision)
    return capture


def _adapt_legacy_capture_media_urls(capture: dict[str, Any]) -> None:
    def adapt_item(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        source_url = value.get("url")
        normalized_url = normalize_amazon_media_url(source_url)
        if not normalized_url:
            return value
        adapted = dict(value)
        adapted["url"] = normalized_url
        return adapted

    media = dict(capture["media"])
    media["main_image"] = adapt_item(media.get("main_image"))
    gallery = media.get("gallery_images")
    if isinstance(gallery, list):
        media["gallery_images"] = [adapt_item(item) for item in gallery]
    capture["media"] = media

    evidence = dict(capture["field_evidence"])
    for path, value in (
        ("media.main_image", media.get("main_image")),
        ("media.gallery_images", media.get("gallery_images")),
    ):
        item = evidence.get(path)
        if isinstance(item, dict):
            adapted_evidence = dict(item)
            adapted_evidence["value"] = value
            evidence[path] = adapted_evidence
    capture["field_evidence"] = evidence


def _validate_field_evidence_item(
    capture: Mapping[str, Any],
    path: str,
    item: Mapping[str, Any],
) -> None:
    required_keys = {"value", "status", "source_kind", "source_locator", "confidence"}
    missing_keys = sorted(required_keys - set(item))
    if missing_keys:
        raise _InvalidCapture(
            f"field_evidence metadata is incomplete for {path}: missing={missing_keys}."
        )
    status = item.get("status")
    if status not in _EVIDENCE_STATUSES:
        raise _InvalidCapture(f"field_evidence status is invalid for {path}.")
    actual_value = _capture_value_at_path(capture, path)
    if not _json_values_equal(item.get("value"), actual_value):
        raise _InvalidCapture(f"field_evidence value does not match capture field {path}.")
    if status == "missing" and not _is_missing_capture_value(path, actual_value):
        raise _InvalidCapture(f"field_evidence missing status contradicts capture field {path}.")

    source_kind = item.get("source_kind")
    source_locator = item.get("source_locator")
    confidence = item.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise _InvalidCapture(f"field_evidence confidence is invalid for {path}.")
    normalized_confidence = float(confidence)
    if not math.isfinite(normalized_confidence) or not 0 <= normalized_confidence <= 1:
        raise _InvalidCapture(f"field_evidence confidence is invalid for {path}.")

    has_source_kind = isinstance(source_kind, str) and bool(source_kind.strip())
    has_source_locator = isinstance(source_locator, str) and bool(source_locator.strip())
    if source_kind is None and source_locator is None:
        if status != "missing" or normalized_confidence != 0:
            raise _InvalidCapture(f"field_evidence source metadata is invalid for {path}.")
        return
    if not has_source_kind or not has_source_locator or normalized_confidence <= 0:
        raise _InvalidCapture(f"field_evidence source metadata is invalid for {path}.")


def _capture_value_at_path(capture: Mapping[str, Any], path: str) -> Any:
    value: Any = capture
    for segment in path.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            raise _InvalidCapture(f"field_evidence path does not resolve in capture: {path}.")
        value = value[segment]
    return value


def _json_values_equal(left: Any, right: Any) -> bool:
    try:
        return json.dumps(
            left,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) == json.dumps(
            right,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return False


def _is_missing_capture_value(path: str, value: Any) -> bool:
    if value is None or value == "" or value == [] or value == {}:
        return True
    return path == "commerce.availability_status" and value == "unknown"


def _validate_capture_sections(
    capture: dict[str, Any],
    *,
    contract_revision: int,
) -> None:
    product = dict(capture["product"])
    commerce = dict(capture["commerce"])
    variants = dict(capture["variants"])
    media = dict(capture["media"])

    for key in ("title", "brand", "description"):
        _validate_optional_text(product.get(key), f"product.{key}")
    _validate_text_list(product.get("category_path"), "product.category_path")
    _validate_text_list(product.get("bullet_points"), "product.bullet_points")
    _validate_string_mapping(product.get("technical_details"), "product.technical_details")

    availability = _clean_text(commerce.get("availability_status"))
    if availability not in _AVAILABILITY_STATUSES:
        raise _InvalidCapture("Normalized capture commerce.availability_status is invalid.")
    _validate_optional_number(commerce.get("rating"), "commerce.rating", minimum=0, maximum=5)
    _validate_optional_integer(commerce.get("review_count"), "commerce.review_count", minimum=0)
    _validate_optional_text(
        commerce.get("bought_past_month"),
        "commerce.bought_past_month",
    )
    bought_past_month = commerce.get("bought_past_month")
    if bought_past_month is not None and not _BOUGHT_PAST_MONTH_VALUE_PATTERN.fullmatch(
        bought_past_month
    ):
        raise _InvalidCapture(
            "Normalized capture commerce.bought_past_month must contain only the Amazon display value."
        )
    if not isinstance(commerce.get("featured_offer"), dict):
        raise _InvalidCapture("Normalized capture commerce.featured_offer must be an object.")
    featured_offer = dict(commerce["featured_offer"])
    for key in (
        "seller_id",
        "seller_name",
        "currency",
        "fulfillment_channel",
        "delivery_text",
        "coupon_text",
    ):
        _validate_optional_text(featured_offer.get(key), f"commerce.featured_offer.{key}")
    if featured_offer.get("is_buy_box") is not None and not isinstance(
        featured_offer.get("is_buy_box"), bool
    ):
        raise _InvalidCapture("commerce.featured_offer.is_buy_box must be boolean or null.")
    _validate_optional_number(
        featured_offer.get("price_amount"),
        "commerce.featured_offer.price_amount",
        minimum=0,
    )
    _validate_optional_number(
        featured_offer.get("list_price_amount"),
        "commerce.featured_offer.list_price_amount",
        minimum=0,
    )
    fulfillment = featured_offer.get("fulfillment_channel")
    if fulfillment is not None and fulfillment not in _FULFILLMENT_CHANNELS:
        raise _InvalidCapture("commerce.featured_offer.fulfillment_channel is invalid.")
    if contract_revision == 1:
        _validate_text_list(
            featured_offer.get("promotions"),
            "commerce.featured_offer.promotions",
        )
    else:
        _validate_structured_promotions(featured_offer.get("promotions"))

    parent_asin = _optional_asin(variants.get("parent_asin"))
    child_asins = _asin_list(variants.get("child_asins"))
    _validate_string_mapping(
        variants.get("current_attributes"),
        "variants.current_attributes",
    )
    _validate_dimension_mapping(variants.get("dimensions"), "variants.dimensions")

    for ranking in capture["rankings"]:
        if not isinstance(ranking, dict):
            raise _InvalidCapture("Every normalized capture ranking must be an object.")
        _required_text(ranking.get("category_name"), "rankings.category_name")
        _validate_text_list(ranking.get("category_path"), "rankings.category_path")
        _validate_required_integer(ranking.get("rank"), "rankings.rank", minimum=1)

    _validate_optional_media_item(media.get("main_image"), "media.main_image")
    gallery_images = media.get("gallery_images")
    if not isinstance(gallery_images, list):
        raise _InvalidCapture("Normalized capture media.gallery_images must be an array.")
    for index, item in enumerate(gallery_images):
        _validate_optional_media_item(item, f"media.gallery_images[{index}]", allow_none=False)

    requested_asin = capture["requested_asin"]
    resolved_asin = capture["resolved_asin"]
    if requested_asin != resolved_asin and (
        parent_asin != requested_asin or resolved_asin not in child_asins
    ):
        raise _InvalidCapture(
            "Resolved ASIN may differ only for a requested parent that contains the child ASIN."
        )
    if requested_asin != resolved_asin:
        _validate_parent_redirect_suppression(capture)

    unavailable_capture = capture["collection_status"] == "unavailable"
    if unavailable_capture != (availability == "unavailable"):
        raise _InvalidCapture(
            "collection_status=unavailable must match commerce.availability_status."
        )


def _validate_materialized_media_assets(
    payload: dict[str, Any],
    capture: dict[str, Any],
    artifact_policy: Mapping[str, str],
) -> tuple[list[dict[str, Any]], dict[str, int | bool]]:
    required_media = _required_materialized_media(capture)
    assets = coerce_mapping_list(payload.get("materialized_media_assets"))
    expected_bucket = artifact_policy["bucket"]
    expected_prefix = join_object_key(
        artifact_policy.get("object_prefix", ""),
        f"product-media/amazon/us/{capture['requested_asin']}",
    ).rstrip("/")
    provided_media: set[tuple[str, str, int]] = set()
    provided_coordinates: set[tuple[str, int]] = set()
    for asset in assets:
        bucket = _required_text(asset.get("bucket"), "materialized media bucket")
        object_key = _required_text(asset.get("object_key"), "materialized media object_key")
        remote_uri = _required_text(asset.get("remote_uri"), "materialized media remote_uri")
        source_url = _required_text(asset.get("source_url"), "materialized media source_url")
        if normalize_amazon_media_url(source_url) != source_url:
            raise ValueError("Materialized media source_url must be a normalized Amazon CDN URL.")
        media_role = _required_text(asset.get("media_role"), "materialized media role")
        position = _validate_required_integer(
            asset.get("position"),
            "materialized media position",
            minimum=0,
        )
        if bucket != expected_bucket:
            raise ValueError("Materialized media must use the configured Amazon artifact bucket.")
        if not object_key.startswith(f"{expected_prefix}/"):
            raise ValueError("Materialized media object_key is outside the governed Amazon prefix.")
        if remote_uri != f"s3://{bucket}/{object_key}":
            raise ValueError("Materialized media remote_uri does not match bucket/object_key.")
        if media_role not in _MATERIALIZED_MEDIA_ROLES:
            raise ValueError("Materialized media role is not allowed for Amazon product facts.")
        if asset.get("sync_state") not in _MATERIALIZED_MEDIA_STATES:
            raise ValueError("Materialized media must come from a successful object-store sync.")
        content_digest = _required_text(
            asset.get("content_digest"),
            "materialized media content_digest",
        ).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", content_digest):
            raise ValueError("Materialized media content_digest must be a SHA-256 digest.")
        object_file_name = object_key.rsplit("/", 1)[-1]
        object_digest = object_file_name.rsplit(".", 1)[0].lower()
        if object_digest != content_digest:
            raise ValueError(
                "Materialized media object_key must be addressed by its content digest."
            )
        mime_type = _required_text(
            asset.get("mime_type"),
            "materialized media mime_type",
        ).lower()
        if mime_type not in _MATERIALIZED_MEDIA_MIME_TYPES:
            raise ValueError("Materialized media MIME type is not an allowed image type.")
        size_bytes = _validate_required_integer(
            asset.get("size_bytes"),
            "materialized media size_bytes",
            minimum=1,
        )
        if size_bytes > _MAX_MATERIALIZED_MEDIA_BYTES:
            raise ValueError("Materialized media exceeds the governed size limit.")
        asset["content_digest"] = content_digest
        asset["mime_type"] = mime_type
        asset["size_bytes"] = size_bytes
        asset_key = _clean_text(asset.get("asset_key"))
        if asset_key and asset_key != f"content_sha256:{content_digest}":
            raise ValueError("Materialized media asset_key does not match content_digest.")
        media_key = (source_url, media_role, position)
        coordinate = (media_role, position)
        if coordinate in provided_coordinates:
            raise ValueError("Materialized media contains a duplicate role/position mapping.")
        provided_media.add(media_key)
        provided_coordinates.add(coordinate)
        if asset.get("metadata") is not None and not isinstance(asset.get("metadata"), dict):
            raise ValueError("Materialized media metadata must be an object.")
        if not any(
            _clean_text(asset.get(key)) for key in ("asset_key", "content_digest", "source_url")
        ):
            raise ValueError(
                "Materialized media assets require asset_key, content_digest, or source_url."
            )
    extra = sorted(provided_media - required_media)
    if extra:
        raise ValueError(
            f"Materialized media contains images not observed in the Amazon capture: extra={extra}."
        )
    missing_count = len(required_media - provided_media)
    return assets, {
        "expected": len(required_media),
        "materialized": len(provided_media),
        "missing": missing_count,
        "complete": missing_count == 0,
    }


def _verify_materialized_media_objects(
    artifact_store: Any,
    assets: list[dict[str, Any]],
) -> None:
    for asset in assets:
        payload = _read_artifact_bytes(
            artifact_store,
            bucket=asset["bucket"],
            object_key=asset["object_key"],
            max_bytes=_MAX_MATERIALIZED_MEDIA_BYTES,
        )
        if not isinstance(payload, (bytes, bytearray)):
            raise ValueError("Materialized media object storage must return bytes.")
        media_bytes = bytes(payload)
        if len(media_bytes) > _MAX_MATERIALIZED_MEDIA_BYTES:
            raise ValueError("Materialized media exceeds the governed size limit.")
        if len(media_bytes) != asset["size_bytes"]:
            raise ValueError("Materialized media size does not match stored bytes.")
        if hashlib.sha256(media_bytes).hexdigest() != asset["content_digest"]:
            raise ValueError("Materialized media digest does not match stored bytes.")
        if not _matches_image_mime(media_bytes, asset["mime_type"]):
            raise ValueError("Materialized media bytes do not match the declared MIME type.")


def _matches_image_mime(payload: bytes, mime_type: str) -> bool:
    if mime_type == "image/jpeg":
        return payload.startswith(b"\xff\xd8\xff")
    if mime_type == "image/png":
        return payload.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/gif":
        return payload.startswith((b"GIF87a", b"GIF89a"))
    if mime_type == "image/webp":
        return len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"
    return False


def _read_artifact_bytes(
    artifact_store: Any,
    *,
    bucket: str,
    object_key: str,
    max_bytes: int,
) -> bytes:
    try:
        payload = artifact_store.read_bytes(
            bucket=bucket,
            object_key=object_key,
            max_bytes=max_bytes,
        )
    except TypeError as exc:
        if "max_bytes" not in str(exc):
            raise
        payload = artifact_store.read_bytes(bucket=bucket, object_key=object_key)
    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError("Artifact object storage must return bytes.")
    result = bytes(payload)
    if len(result) > max_bytes:
        raise ValueError("Artifact object exceeds the governed size limit.")
    return result


def _required_materialized_media(capture: Mapping[str, Any]) -> set[tuple[str, str, int]]:
    media = _mapping_value(capture.get("media"))
    evidence = _mapping_value(capture.get("field_evidence"))
    required: set[tuple[str, str, int]] = set()
    if _is_strictly_observed(evidence, "media.main_image"):
        main_url = _media_url(media.get("main_image"), "media.main_image")
        required.add((main_url, "main_image", 0))
    if _is_strictly_observed(evidence, "media.gallery_images"):
        for index, item in enumerate(_list_value(media.get("gallery_images"))):
            gallery_url = _media_url(item, f"media.gallery_images[{index}]")
            required.add((gallery_url, "gallery_image", index))
    return required


def _effective_collection_status(
    capture_status: str,
    media_coverage: Mapping[str, int | bool],
) -> str:
    if capture_status == "unavailable":
        return "unavailable"
    if capture_status == "partial_success" or media_coverage.get("complete") is not True:
        return "partial_success"
    return "success"


def _suppress_incomplete_media_projection(
    projection_facts: dict[str, Any],
    *,
    capture: Mapping[str, Any],
    materialized_media_assets: list[dict[str, Any]],
) -> None:
    required_media = _required_materialized_media(capture)
    provided_media = {
        (
            _clean_text(asset.get("source_url")),
            _clean_text(asset.get("media_role")),
            int(asset.get("position") or 0),
        )
        for asset in materialized_media_assets
    }
    evidence = dict(_mapping_value(projection_facts.get("field_evidence")))
    required_main = {item for item in required_media if item[1] == "main_image"}
    required_gallery = {item for item in required_media if item[1] == "gallery_image"}
    if required_main and not required_main.issubset(provided_media):
        _mark_projection_evidence_missing(evidence, "media.main_image")
    if required_gallery and not required_gallery.issubset(provided_media):
        _mark_projection_evidence_missing(evidence, "media.gallery_images")
    projection_facts["field_evidence"] = evidence


def _mark_projection_evidence_missing(evidence: dict[str, Any], path: str) -> None:
    item = evidence.get(path)
    normalized = dict(item) if isinstance(item, Mapping) else {}
    normalized["status"] = "missing"
    evidence[path] = normalized


def _validate_source_binding(payload: dict[str, Any]) -> dict[str, str]:
    source_table_ref = coerce_mapping(payload.get("source_table_ref"))
    return {
        "base_id": _required_text(source_table_ref.get("base_id"), "source_table_ref.base_id"),
        "table_id": _required_text(
            source_table_ref.get("table_id"),
            "source_table_ref.table_id",
        ),
    }


def _persist_capture(
    *,
    context: HandlerContext,
    payload: dict[str, Any],
    capture: dict[str, Any],
    capture_bytes: bytes,
    normalized_ref: dict[str, Any],
    raw_capture_refs: list[dict[str, Any]],
    source_table_ref: dict[str, str],
    source_record_id: str,
    run_id: str,
    fact_store: Any,
) -> dict[str, Any]:
    product = dict(capture["product"])
    commerce = dict(capture["commerce"])
    variants = dict(capture["variants"])
    media = dict(capture["media"])
    evidence = dict(capture["field_evidence"])
    observed_at = capture["captured_at_epoch"]
    requested_asin = capture["requested_asin"]
    resolved_asin = capture["resolved_asin"]
    parent_asin = _optional_asin(variants.get("parent_asin"))
    master_parent_asin = parent_asin if parent_asin and parent_asin != requested_asin else None

    capture_digest = hashlib.sha256(capture_bytes).hexdigest()
    origin_request_id = normalized_ref["request_id"]
    origin_execution_id = normalized_ref["execution_id"]
    identity_row = fact_store.ensure_product_identity(
        marketplace_code="US",
        asin=requested_asin,
        canonical_url=capture["canonical_url"],
        observed_at=observed_at,
    )
    product_id = identity_row["id"]
    field_coverage = _field_coverage(evidence)
    snapshot_payload = {
        "collection_status": capture["collection_status"],
        "profile_context": _safe_profile_context(capture.get("profile_context")),
    }
    if _is_strictly_observed(evidence, "commerce.bought_past_month"):
        snapshot_payload["bought_past_month"] = commerce.get("bought_past_month")
    snapshot = fact_store.record_product_snapshot(
        product_id=product_id,
        marketplace_code="US",
        asin=requested_asin,
        run_id=run_id,
        request_id=origin_request_id,
        execution_id=origin_execution_id,
        resolved_asin=resolved_asin,
        parent_asin=parent_asin or "",
        availability_status=_clean_text(commerce.get("availability_status")) or "unknown",
        title=product.get("title") or "",
        brand=product.get("brand") or "",
        category_path=_list_value(product.get("category_path")),
        bullet_points=_list_value(product.get("bullet_points")),
        description=product.get("description") or "",
        technical_details=_mapping_value(product.get("technical_details")),
        rating=commerce.get("rating"),
        review_count=commerce.get("review_count"),
        variant_attributes=_mapping_value(variants.get("current_attributes")),
        child_asins=_asin_list(variants.get("child_asins")),
        field_coverage=field_coverage,
        payload=snapshot_payload,
        content_digest=capture_digest,
        collected_at=observed_at,
    )
    snapshot_id = snapshot["snapshot_id"]

    raw_capture_ids: list[str] = []
    for raw_ref in raw_capture_refs:
        raw_row = fact_store.record_raw_capture(
            product_id=product_id,
            snapshot_id=snapshot_id,
            capture_kind=raw_ref["capture_kind"],
            bucket=raw_ref["bucket"],
            object_key=raw_ref["object_key"],
            content_digest=raw_ref["content_digest"],
            content_type=raw_ref["content_type"],
            request_id=raw_ref["request_id"],
            execution_id=raw_ref["execution_id"],
            run_id=run_id,
            sanitization_status=raw_ref["sanitization_status"],
            collected_at=observed_at,
        )
        raw_capture_ids.append(raw_row["raw_capture_id"])

    product_row = fact_store.upsert_product(
        marketplace_code="US",
        asin=requested_asin,
        canonical_url=capture["canonical_url"],
        parent_asin=(
            master_parent_asin if _is_observed(evidence, "variants.parent_asin") else None
        ),
        title=_observed_value(evidence, "product.title", product.get("title")),
        brand=_observed_value(evidence, "product.brand", product.get("brand")),
        category_path=_observed_value(
            evidence,
            "product.category_path",
            product.get("category_path"),
        ),
        status=_product_master_status(capture, evidence),
        facts={
            "collection_status": capture["collection_status"],
            "resolved_asin": resolved_asin,
        },
        observed_at=observed_at,
    )
    if product_row["id"] != product_id:
        raise AmazonFactCollisionError(
            "Amazon product identity resolved to a different product row.",
            error_code="amazon_product_identity_collision",
        )
    offer_count = 0
    featured_offer = _mapping_value(commerce.get("featured_offer"))
    if _has_observed_offer(evidence):
        fact_store.record_featured_offer(
            product_snapshot_id=snapshot_id,
            product_id=product_id,
            seller_id=featured_offer.get("seller_id") or "",
            seller_name=featured_offer.get("seller_name") or "",
            is_featured_offer=featured_offer.get("is_buy_box") is True,
            price_amount=featured_offer.get("price_amount"),
            list_price_amount=featured_offer.get("list_price_amount"),
            currency=featured_offer.get("currency") or "",
            availability_status=commerce.get("availability_status") or "unknown",
            fulfillment_channel=featured_offer.get("fulfillment_channel") or "unknown",
            delivery_text=featured_offer.get("delivery_text") or "",
            coupon_text=featured_offer.get("coupon_text") or "",
            promotions=_list_value(featured_offer.get("promotions")),
            profile_context_digest=_clean_text(
                _mapping_value(capture.get("profile_context")).get("profile_context_digest")
            ),
            collected_at=observed_at,
        )
        offer_count = 1

    variant_count = 0
    current_attributes = _observed_value(
        evidence,
        "variants.current_attributes",
        _mapping_value(variants.get("current_attributes")),
    )
    dimensions = _observed_value(
        evidence,
        "variants.dimensions",
        _mapping_value(variants.get("dimensions")),
    )
    if parent_asin:
        for child_asin in _asin_list(variants.get("child_asins")):
            fact_store.upsert_variant(
                marketplace_code="US",
                parent_asin=parent_asin,
                child_asin=child_asin,
                attributes=(current_attributes if child_asin == resolved_asin else None),
                dimensions=dimensions,
                source_asin=resolved_asin,
                observed_at=observed_at,
            )
            variant_count += 1

    bsr_count = 0
    for ranking in coerce_mapping_list(capture.get("rankings")):
        category_name = _clean_text(ranking.get("category_name"))
        rank_value = ranking.get("rank")
        if not category_name or rank_value in (None, ""):
            continue
        fact_store.record_bsr_snapshot(
            product_snapshot_id=snapshot_id,
            product_id=product_id,
            category_name=category_name,
            category_path=_list_value(ranking.get("category_path")),
            rank_value=rank_value,
            collected_at=observed_at,
        )
        bsr_count += 1

    media_asset_count = 0
    media_relation_count = 0
    for asset in coerce_mapping_list(payload.get("materialized_media_assets")):
        if not _clean_text(asset.get("bucket")) or not _clean_text(asset.get("object_key")):
            raise ValueError("Materialized media assets require bucket and object_key.")
        asset_row = fact_store.upsert_media_asset(
            source_url=asset.get("source_url") or "",
            content_digest=asset.get("content_digest") or "",
            bucket=asset.get("bucket") or "",
            object_key=asset.get("object_key") or "",
            remote_uri=asset.get("remote_uri") or "",
            file_name=asset.get("file_name") or "",
            mime_type=asset.get("mime_type") or "",
            size_bytes=asset.get("size_bytes") or 0,
            metadata=_mapping_value(asset.get("metadata")),
            asset_key=asset.get("asset_key") or "",
            observed_at=observed_at,
        )
        fact_store.link_product_media_asset(
            product_id=product_id,
            asset_id=asset_row["asset_id"],
            media_role=_required_text(asset.get("media_role"), "media_role"),
            position=asset.get("position") or 0,
            metadata={"source": "amazon_product_capture"},
            observed_at=observed_at,
        )
        media_asset_count += 1
        media_relation_count += 1

    binding = fact_store.upsert_feishu_binding(
        product_id=product_id,
        base_id=source_table_ref["base_id"],
        table_id=source_table_ref["table_id"],
        record_id=source_record_id,
        source_asin=requested_asin,
        status="facts_persisted",
        latest_snapshot_id=snapshot_id,
        observed_at=observed_at,
    )
    fact_store.set_latest_snapshot(
        product_id=product_id,
        snapshot_id=snapshot_id,
        observed_at=observed_at,
    )

    persisted_counts = {
        "products": 1,
        "product_snapshots": 1,
        "offer_snapshots": offer_count,
        "variant_relations": variant_count,
        "bsr_snapshots": bsr_count,
        "media_assets": media_asset_count,
        "media_relations": media_relation_count,
        "raw_captures": len(raw_capture_ids),
        "feishu_bindings": 1,
    }
    return {
        "product_id": product_id,
        "snapshot_id": snapshot_id,
        "binding_id": binding["binding_id"],
        "raw_capture_ids": raw_capture_ids,
        "normalized_capture_ref": dict(normalized_ref),
        "persisted_counts": persisted_counts,
        "projection_facts": {
            "source_record_id": source_record_id,
            "requested_asin": requested_asin,
            "resolved_asin": resolved_asin,
            "canonical_url": capture["canonical_url"],
            "captured_at": capture["captured_at"],
            "collection_status": capture["collection_status"],
            "product": product,
            "commerce": commerce,
            "variants": variants,
            "rankings": list(capture["rankings"]),
            "media": media,
            "field_evidence": evidence,
        },
    }


def _field_coverage(evidence: Mapping[str, Any]) -> dict[str, Any]:
    total = len(evidence)
    observed = sum(
        1
        for item in evidence.values()
        if isinstance(item, dict) and item.get("status") in {"observed", "explicitly_unavailable"}
    )
    missing = total - observed
    return {
        "total": total,
        "observed": observed,
        "missing": missing,
        "percent": round((observed / total) * 100.0, 2) if total else 0.0,
    }


def _has_observed_offer(evidence: Mapping[str, Any]) -> bool:
    return any(
        path.startswith("commerce.featured_offer.")
        and isinstance(item, dict)
        and item.get("status") == "observed"
        for path, item in evidence.items()
    )


def _product_master_status(capture: Mapping[str, Any], evidence: Mapping[str, Any]) -> str | None:
    if capture.get("collection_status") == "unavailable":
        return "unavailable"
    if _is_observed(evidence, "commerce.availability_status"):
        return "active"
    return None


def _is_observed(evidence: Mapping[str, Any], path: str) -> bool:
    item = evidence.get(path)
    return isinstance(item, dict) and item.get("status") in {
        "observed",
        "explicitly_unavailable",
    }


def _is_strictly_observed(evidence: Mapping[str, Any], path: str) -> bool:
    item = evidence.get(path)
    return isinstance(item, dict) and item.get("status") == "observed"


def _observed_value(evidence: Mapping[str, Any], path: str, value: Any) -> Any:
    return value if _is_observed(evidence, path) else None


def _safe_profile_context(value: Any) -> dict[str, Any]:
    profile_context = _mapping_value(value)
    return {
        key: profile_context[key]
        for key in ("locale", "currency", "delivery_region", "profile_context_digest")
        if key in profile_context
    }


def _mapping_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list_value(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _asin_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        asin = normalize_asin(item)
        if asin not in result:
            result.append(asin)
    return result


def _optional_asin(value: Any) -> str:
    if value in (None, ""):
        return ""
    return normalize_asin(value)


def _validate_parent_redirect_suppression(capture: Mapping[str, Any]) -> None:
    expected_product = {
        "title": None,
        "brand": None,
        "category_path": [],
        "bullet_points": [],
        "description": None,
        "technical_details": {},
    }
    expected_offer = {
        "seller_id": None,
        "seller_name": None,
        "is_buy_box": None,
        "price_amount": None,
        "list_price_amount": None,
        "currency": None,
        "fulfillment_channel": None,
        "delivery_text": None,
        "coupon_text": None,
        "promotions": [],
    }
    commerce = _mapping_value(capture.get("commerce"))
    if _mapping_value(capture.get("product")) != expected_product:
        raise _InvalidCapture("Parent redirect must suppress child product facts.")
    if (
        commerce.get("availability_status") != "unknown"
        or commerce.get("rating") is not None
        or commerce.get("review_count") is not None
        or _mapping_value(commerce.get("featured_offer")) != expected_offer
        or capture.get("rankings") != []
        or _mapping_value(capture.get("media")) != {"main_image": None, "gallery_images": []}
    ):
        raise _InvalidCapture("Parent redirect must suppress child commerce and media facts.")
    evidence = _mapping_value(capture.get("field_evidence"))
    for path, item in evidence.items():
        if path.startswith(("product.", "commerce.", "media.")) or path == "rankings":
            if _mapping_value(item).get("status") != "missing":
                raise _InvalidCapture("Parent redirect evidence must mark child facts as missing.")


def _validate_optional_text(value: Any, field_name: str) -> None:
    if value is not None and not isinstance(value, str):
        raise _InvalidCapture(f"Normalized capture {field_name} must be text or null.")


def _validate_text_list(value: Any, field_name: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _InvalidCapture(f"Normalized capture {field_name} must be a text array.")


def _validate_structured_promotions(value: Any) -> None:
    field_name = "commerce.featured_offer.promotions"
    if not isinstance(value, list):
        raise _InvalidCapture(f"Normalized capture {field_name} must be an object array.")
    for item in value:
        if not isinstance(item, dict) or set(item) != _PROMOTION_KEYS:
            raise _InvalidCapture(
                f"Every normalized capture {field_name} item must use the exact revision 2 keys."
            )
        if item.get("promotion_type") not in _PROMOTION_TYPES:
            raise _InvalidCapture(f"Normalized capture {field_name} promotion_type is invalid.")
        if item.get("discount_type") not in _PROMOTION_DISCOUNT_TYPES:
            raise _InvalidCapture(f"Normalized capture {field_name} discount_type is invalid.")
        if item.get("reference_price_type") is not None:
            raise _InvalidCapture(
                f"Normalized capture {field_name} reference_price_type must be null."
            )
        for key in ("label", "raw_text"):
            text_value = item.get(key)
            if not isinstance(text_value, str) or not text_value.strip():
                raise _InvalidCapture(
                    f"Normalized capture {field_name} {key} must be non-empty text."
                )
            if _PROMOTION_SENSITIVE_PATTERN.search(text_value):
                raise _InvalidCapture(
                    f"Normalized capture {field_name} contains sensitive promotion text."
                )
        for key in ("discount_value", "deal_price", "reference_price"):
            _validate_optional_number(item.get(key), f"{field_name}.{key}", minimum=0)
        currency = item.get("currency")
        if currency is not None and currency != "USD":
            raise _InvalidCapture(f"Normalized capture {field_name} currency is invalid.")
        for key in ("prime_only", "claim_required"):
            if not isinstance(item.get(key), bool):
                raise _InvalidCapture(f"Normalized capture {field_name} {key} must be boolean.")
        if item.get("prime_only") is not False:
            raise _InvalidCapture(f"Normalized capture {field_name} prime_only must be false.")
        if item.get("promotion_type") == "coupon" and any(
            item.get(key) is not None
            for key in ("deal_price", "reference_price", "reference_price_type")
        ):
            raise _InvalidCapture(
                f"Normalized capture {field_name} coupon price fields must be null."
            )
        if item.get("promotion_type") == "coupon" and (
            item.get("discount_type") not in {"percentage", "amount"}
            or item.get("discount_value") is None
        ):
            raise _InvalidCapture(f"Normalized capture {field_name} coupon discount is invalid.")
        if item.get("promotion_type") == "limited_time_deal" and any(
            item.get(key) is not None
            for key in ("discount_value", "reference_price", "reference_price_type")
        ):
            raise _InvalidCapture(
                f"Normalized capture {field_name} limited time deal comparison fields must be null."
            )
        if item.get("promotion_type") == "limited_time_deal" and (
            item.get("discount_type") != "price_override" or item.get("deal_price") is None
        ):
            raise _InvalidCapture(
                f"Normalized capture {field_name} limited time deal price is invalid."
            )


def _validate_string_mapping(value: Any, field_name: str) -> None:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise _InvalidCapture(f"Normalized capture {field_name} must be a text mapping.")


def _validate_dimension_mapping(value: Any, field_name: str) -> None:
    if not isinstance(value, dict):
        raise _InvalidCapture(f"Normalized capture {field_name} must be an object.")
    for key, items in value.items():
        if (
            not isinstance(key, str)
            or not isinstance(items, list)
            or any(not isinstance(item, str) for item in items)
        ):
            raise _InvalidCapture(
                f"Normalized capture {field_name} must map text keys to text arrays."
            )


def _validate_optional_media_item(
    value: Any,
    field_name: str,
    *,
    allow_none: bool = True,
) -> None:
    if value is None and allow_none:
        return
    _media_url(value, field_name)


def _media_url(value: Any, field_name: str) -> str:
    if not isinstance(value, dict):
        raise _InvalidCapture(f"Normalized capture {field_name} must be a media object.")
    source_url = _required_text(value.get("url"), f"{field_name}.url")
    if normalize_amazon_media_url(source_url) != source_url:
        raise _InvalidCapture(
            f"Normalized capture {field_name}.url must be a normalized Amazon CDN URL."
        )
    return source_url


def _validate_optional_number(
    value: Any,
    field_name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InvalidCapture(f"Normalized capture {field_name} must be numeric or null.")
    number = float(value)
    if not math.isfinite(number):
        raise _InvalidCapture(f"Normalized capture {field_name} must be finite.")
    if minimum is not None and number < minimum:
        raise _InvalidCapture(f"Normalized capture {field_name} is below its minimum.")
    if maximum is not None and number > maximum:
        raise _InvalidCapture(f"Normalized capture {field_name} exceeds its maximum.")


def _validate_optional_integer(
    value: Any,
    field_name: str,
    *,
    minimum: int | None = None,
) -> None:
    if value is None:
        return
    _validate_required_integer(value, field_name, minimum=minimum)


def _validate_required_integer(
    value: Any,
    field_name: str,
    *,
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _InvalidCapture(f"Normalized capture {field_name} must be an integer.")
    if minimum is not None and value < minimum:
        raise _InvalidCapture(f"Normalized capture {field_name} is below its minimum.")
    return value


def _iso_timestamp(value: Any) -> float:
    text = _required_text(value, "captured_at")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _configuration_failure(
    context: HandlerContext,
    *,
    error_code: str,
    message: str,
) -> HandlerResult:
    return failed_result(
        context,
        error=build_error(
            error_type="persistence_configuration_missing",
            error_code=error_code,
            message=message,
            retryable=False,
            details={"required": True, "configured": False},
        ),
        summary={"persistence_mode": "missing_configuration"},
    )


def _validation_failure(
    context: HandlerContext,
    *,
    error_code: str,
    message: str,
) -> HandlerResult:
    return failed_result(
        context,
        error=build_error(
            error_type="invalid_input",
            error_code=error_code,
            message=message,
            retryable=False,
        ),
        summary={"persistence_mode": "rejected"},
    )


def _required_sha256_digest(value: Any, field_name: str) -> str:
    digest = _required_text(value, field_name).lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{field_name} must be a 64-character SHA-256 hex digest.")
    return digest


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text.")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} is required.")
    return cleaned


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


__all__ = ["CONTRACT", "HANDLER_CODE", "amazon_product_fact_upsert_handler"]
