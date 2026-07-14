from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Mapping

from automation_business_scaffold.capabilities.browser.amazon.product_page import (
    AmazonProductExtractionError,
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
from automation_business_scaffold.infrastructure.facts.amazon_fact_store import AmazonFactStore


HANDLER_CODE = "amazon_product_fact_upsert"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]
_COLLECTION_STATUSES = {"success", "partial_success", "unavailable"}
_EVIDENCE_STATUSES = {"observed", "missing", "explicitly_unavailable"}
_AVAILABILITY_STATUSES = {"in_stock", "out_of_stock", "unavailable", "unknown"}
_FULFILLMENT_CHANNELS = {"amazon", "merchant", "unknown"}
_MATERIALIZED_MEDIA_STATES = {"uploaded", "reused", "reused_in_run"}
_MATERIALIZED_MEDIA_ROLES = {"main_image", "gallery_image"}


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
        capture_bytes = artifact_store.read_bytes(
            bucket=normalized_ref["bucket"],
            object_key=normalized_ref["object_key"],
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
        source_table_ref = _validate_source_binding(payload)
        run_id = _required_text(payload.get("run_id"), "run_id")
        source_record_id = _required_text(payload.get("source_record_id"), "source_record_id")
        _validate_raw_capture_identity(raw_capture_refs, capture, artifact_policy)
        payload["materialized_media_assets"] = _validate_materialized_media_assets(
            payload,
            capture,
            artifact_policy,
        )
    except (
        AmazonProductExtractionError,
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
            fact_store=fact_store,
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

    if context.metadata.get("include_transient_projection_facts") is not True:
        persisted.pop("projection_facts", None)

    summary = {
        "collection_status": capture["collection_status"],
        "marketplace_code": capture["marketplace_code"],
        "requested_asin": capture["requested_asin"],
        "persistence_mode": "database",
        "persisted_counts": dict(persisted["persisted_counts"]),
    }
    if capture["collection_status"] == "partial_success":
        return partial_success_result(
            context,
            summary=summary,
            result=persisted,
            warnings=("Amazon capture contains fields without observed evidence.",),
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
    for raw_ref in raw_refs:
        capture_kind = _required_text(raw_ref.get("capture_kind"), "capture_kind")
        validated = _validate_artifact_ref(
            raw_ref,
            expected_kind=capture_kind,
            require_sanitized=capture_kind == "html",
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
    normalized = _validate_artifact_ref(normalized_ref, expected_kind="normalized_capture")
    _validate_governed_artifact_ref(
        normalized,
        artifact_policy=artifact_policy,
        relative_prefix="raw-captures/amazon/us",
    )
    normalized_raw = _validate_artifact_ref(
        normalized_raw,
        expected_kind="normalized_capture",
    )
    if (
        normalized["bucket"],
        normalized["object_key"],
    ) != (
        normalized_raw["bucket"],
        normalized_raw["object_key"],
    ):
        raise ValueError(
            "normalized_capture_ref must identify the normalized_capture raw evidence object."
        )
    return normalized, validated_refs


def _validate_raw_capture_identity(
    raw_capture_refs: list[dict[str, Any]],
    capture: Mapping[str, Any],
    artifact_policy: Mapping[str, str],
) -> None:
    relative_prefix = f"raw-captures/amazon/us/{capture['requested_asin']}"
    for raw_ref in raw_capture_refs:
        _validate_governed_artifact_ref(
            raw_ref,
            artifact_policy=artifact_policy,
            relative_prefix=relative_prefix,
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
    require_sanitized: bool = False,
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
            "collected_at",
            "created_at",
        )
        if key in value
    }
    if _clean_text(ref.get("capture_kind")) != expected_kind:
        raise ValueError(f"Artifact evidence must use capture_kind={expected_kind}.")
    ref["bucket"] = _required_text(ref.get("bucket"), "artifact bucket")
    ref["object_key"] = _required_text(ref.get("object_key"), "artifact object_key")
    if require_sanitized and _clean_text(ref.get("sanitization_status")) != "sanitized":
        raise ValueError("HTML raw capture evidence must be sanitized.")
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
    if capture.get("contract_revision") != 1:
        raise _InvalidCapture("Normalized capture contract_revision must equal 1.")
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

    for path, item in capture["field_evidence"].items():
        if not isinstance(path, str) or not isinstance(item, dict):
            raise _InvalidCapture("Every field_evidence entry must be an object keyed by path.")
        if item.get("status") not in _EVIDENCE_STATUSES:
            raise _InvalidCapture(f"field_evidence status is invalid for {path}.")
    _validate_capture_sections(capture)
    return capture


def _validate_capture_sections(capture: dict[str, Any]) -> None:
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
    _validate_text_list(
        featured_offer.get("promotions"),
        "commerce.featured_offer.promotions",
    )

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
) -> list[dict[str, Any]]:
    if capture["collection_status"] == "unavailable" or not _has_observed_media(
        capture["field_evidence"]
    ):
        return []
    required_media = _required_materialized_media(capture)
    assets = coerce_mapping_list(payload.get("materialized_media_assets"))
    expected_bucket = artifact_policy["bucket"]
    expected_prefix = join_object_key(
        artifact_policy.get("object_prefix", ""),
        f"product-media/amazon/us/{capture['requested_asin']}",
    ).rstrip("/")
    provided_media: set[tuple[str, str, int]] = set()
    for asset in assets:
        bucket = _required_text(asset.get("bucket"), "materialized media bucket")
        object_key = _required_text(asset.get("object_key"), "materialized media object_key")
        remote_uri = _required_text(asset.get("remote_uri"), "materialized media remote_uri")
        source_url = _required_text(asset.get("source_url"), "materialized media source_url")
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
        media_key = (source_url, media_role, position)
        if media_key in provided_media:
            raise ValueError("Materialized media contains a duplicate role/position mapping.")
        provided_media.add(media_key)
        if asset.get("size_bytes") is not None:
            _validate_optional_integer(
                asset.get("size_bytes"),
                "materialized media size_bytes",
                minimum=0,
            )
        if asset.get("metadata") is not None and not isinstance(asset.get("metadata"), dict):
            raise ValueError("Materialized media metadata must be an object.")
        if not any(
            _clean_text(asset.get(key)) for key in ("asset_key", "content_digest", "source_url")
        ):
            raise ValueError(
                "Materialized media assets require asset_key, content_digest, or source_url."
            )
    if provided_media != required_media:
        missing = sorted(required_media - provided_media)
        extra = sorted(provided_media - required_media)
        raise ValueError(
            "Materialized media does not exactly cover observed Amazon images: "
            f"missing={missing}, extra={extra}."
        )
    return assets


def _required_materialized_media(capture: Mapping[str, Any]) -> set[tuple[str, str, int]]:
    media = _mapping_value(capture.get("media"))
    evidence = _mapping_value(capture.get("field_evidence"))
    required: set[tuple[str, str, int]] = set()
    main_url = ""
    if _is_observed(evidence, "media.main_image"):
        main_url = _media_url(media.get("main_image"), "media.main_image")
        required.add((main_url, "main_image", 0))
    if _is_observed(evidence, "media.gallery_images"):
        for index, item in enumerate(_list_value(media.get("gallery_images"))):
            gallery_url = _media_url(item, f"media.gallery_images[{index}]")
            if gallery_url == main_url or any(key[0] == gallery_url for key in required):
                continue
            required.add((gallery_url, "gallery_image", index))
    return required


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
    product_id = product_row["id"]
    field_coverage = _field_coverage(evidence)
    snapshot = fact_store.record_product_snapshot(
        product_id=product_id,
        marketplace_code="US",
        asin=requested_asin,
        run_id=run_id,
        request_id=context.request_id,
        execution_id=context.job_id,
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
        payload={
            "collection_status": capture["collection_status"],
            "profile_context": _safe_profile_context(capture.get("profile_context")),
        },
        content_digest=hashlib.sha256(capture_bytes).hexdigest(),
        collected_at=observed_at,
    )
    snapshot_id = snapshot["snapshot_id"]
    fact_store.set_latest_snapshot(
        product_id=product_id,
        snapshot_id=snapshot_id,
        observed_at=observed_at,
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

    raw_capture_ids: list[str] = []
    for raw_ref in raw_capture_refs:
        raw_row = fact_store.record_raw_capture(
            product_id=product_id,
            snapshot_id=snapshot_id,
            capture_kind=_required_text(raw_ref.get("capture_kind"), "capture_kind"),
            bucket=_required_text(raw_ref.get("bucket"), "raw capture bucket"),
            object_key=_required_text(raw_ref.get("object_key"), "raw capture object_key"),
            content_digest=raw_ref.get("content_digest") or "",
            content_type=raw_ref.get("content_type") or "",
            request_id=context.request_id,
            execution_id=context.job_id,
            run_id=run_id,
            sanitization_status=raw_ref.get("sanitization_status") or "unknown",
            collected_at=observed_at,
        )
        raw_capture_ids.append(raw_row["raw_capture_id"])

    binding = fact_store.upsert_feishu_binding(
        product_id=product_id,
        base_id=source_table_ref["base_id"],
        table_id=source_table_ref["table_id"],
        record_id=source_record_id,
        source_asin=requested_asin,
        status="facts_persisted",
        last_synced_snapshot_id=snapshot_id,
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


def _has_observed_media(evidence: Mapping[str, Any]) -> bool:
    return any(
        path.startswith("media.") and isinstance(item, dict) and item.get("status") == "observed"
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
    return _required_text(value.get("url"), f"{field_name}.url")


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
