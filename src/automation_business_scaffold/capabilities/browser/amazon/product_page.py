from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse


_ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")
_ASIN_PATH_PATTERNS = (
    re.compile(r"(?:^|/)dp/([A-Z0-9]{10})(?:/|$)", re.IGNORECASE),
    re.compile(r"(?:^|/)gp/product/([A-Z0-9]{10})(?:/|$)", re.IGNORECASE),
    re.compile(r"(?:^|/)gp/aw/d/([A-Z0-9]{10})(?:/|$)", re.IGNORECASE),
)
_VOID_TAGS = {
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
_OFFER_FIELD_DEFAULTS: Mapping[str, Any] = {
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
_NETWORK_MAX_TEXT_LENGTH = 20_000
_NETWORK_MAX_LIST_ITEMS = 100
_NETWORK_MAX_MAPPING_ITEMS = 200
_NETWORK_EXPLICIT_EMPTY_LIST = object()
_NETWORK_EXPLICIT_EMPTY_MAPPING = object()
_NETWORK_SENSITIVE_KEY_MARKERS = (
    "accesskey",
    "account",
    "address",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "customer",
    "deliverylocation",
    "email",
    "localstorage",
    "password",
    "phone",
    "postal",
    "profile",
    "recipient",
    "requestheader",
    "secret",
    "session",
    "shippinglocation",
    "token",
    "owner",
    "user",
    "workspace",
)
_NETWORK_MEDIA_HOST_SUFFIXES = (
    ".media-amazon.com",
    ".ssl-images-amazon.com",
)


class AmazonProductExtractionError(RuntimeError):
    """Base error for deterministic Amazon product-page extraction failures."""

    error_code = "amazon_product_extraction_failed"

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code or type(self).error_code


class InvalidASINError(AmazonProductExtractionError):
    error_code = "invalid_asin"


class UnsupportedMarketplaceError(AmazonProductExtractionError):
    error_code = "unsupported_marketplace"


class InvalidAmazonProductURLError(AmazonProductExtractionError):
    error_code = "invalid_product_url"


class AmazonIdentityMismatchError(AmazonProductExtractionError):
    error_code = "identity_mismatch"


class AmazonAccessBlockedError(AmazonProductExtractionError):
    error_code = "access_blocked"


@dataclass(slots=True)
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list[_Node] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.roots: list[_Node] = []
        self._stack: list[_Node] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(
            tag=tag.lower(),
            attrs={key.lower(): value or "" for key, value in attrs},
        )
        if self._stack:
            self._stack[-1].children.append(node)
        else:
            self.roots.append(node)
        if node.tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag == normalized:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self._stack:
            self._stack[-1].text_parts.append(data)


@dataclass(frozen=True, slots=True)
class _Candidate:
    value: Any
    source_kind: str
    source_locator: str
    confidence: float
    status: str = "observed"
    accept_empty: bool = False


def normalize_asin(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidASINError("ASIN must be a string")
    asin = value.strip().upper()
    if not _ASIN_PATTERN.fullmatch(asin):
        raise InvalidASINError("ASIN must match ^[A-Z0-9]{10}$")
    return asin


def canonical_amazon_url(asin: object) -> str:
    return f"https://www.amazon.com/dp/{normalize_asin(asin)}"


def extract_asin_from_url(url: object) -> str:
    if not isinstance(url, str) or not url.strip():
        raise InvalidAmazonProductURLError("Amazon product URL must be a non-empty string")
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise InvalidAmazonProductURLError("Amazon product URL must use HTTP or HTTPS")
    if host != "amazon.com" and not host.endswith(".amazon.com"):
        raise UnsupportedMarketplaceError("Only the Amazon US marketplace is supported")
    for pattern in _ASIN_PATH_PATTERNS:
        match = pattern.search(parsed.path)
        if match:
            return normalize_asin(match.group(1))
    raise InvalidAmazonProductURLError("Amazon product URL does not contain a supported ASIN path")


def extract_amazon_product_capture(
    html: str,
    requested_asin: object,
    resolved_url: object,
    observed_at: datetime | str,
    *,
    network_product_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    requested = normalize_asin(requested_asin)
    if not isinstance(html, str):
        raise AmazonProductExtractionError("Amazon page HTML must be a string")
    _raise_if_access_blocked(html)
    resolved_from_url = extract_asin_from_url(resolved_url)

    document = _parse_document(html)
    structured = _extract_structured_product(document)
    state = _extract_embedded_state(document)
    dom = _extract_dom_values(document)
    network = _validated_network_product_data(
        network_product_data,
        expected_asin=resolved_from_url,
    )

    state_asin = _optional_asin(state.get("asin"))
    structured_asin = _optional_asin(structured.get("asin"))
    if state_asin and state_asin != resolved_from_url:
        raise AmazonIdentityMismatchError(
            "Embedded Amazon product identity differs from the resolved URL"
        )
    if structured_asin and structured_asin != resolved_from_url:
        raise AmazonIdentityMismatchError(
            "Structured Amazon product identity differs from the resolved URL"
        )
    resolved = state_asin or resolved_from_url

    state_variants = _mapping(state.get("variants"))
    network_variants = _mapping(network.get("variants"))
    state_parent = _optional_asin(state.get("parent_asin"))
    state_variant_parent = _optional_asin(state_variants.get("parent_asin"))
    network_parent = _optional_asin(network_variants.get("parent_asin"))
    dom_parent = _optional_asin(dom.get("parent_asin"))
    parent = _first_asin(
        state_parent,
        state_variant_parent,
        network_parent,
        dom_parent,
    )
    parent_redirect = requested != resolved and requested == parent
    if requested != resolved and not parent_redirect:
        raise AmazonIdentityMismatchError(
            f"Requested ASIN {requested} resolved to unrelated ASIN {resolved}"
        )

    evidence: dict[str, dict[str, Any]] = {}
    product_state = _mapping(state.get("product"))
    network_product = _mapping(network.get("product"))
    commerce_state = _mapping(state.get("commerce"))
    network_commerce = _mapping(network.get("commerce"))
    offer_state = _mapping(commerce_state.get("featured_offer"))
    network_offer = _mapping(network_commerce.get("featured_offer"))
    media_state = _mapping(state.get("media"))
    network_media = _mapping(network.get("media"))
    structured_offer = _mapping(structured.get("featured_offer"))

    product = {
        "title": _choose(
            "product.title",
            evidence,
            None,
            _candidate(structured.get("title"), "structured_data", "jsonld.Product.name", 0.98),
            _candidate(
                _clean_text(product_state.get("title")),
                "embedded_state",
                "state.product.title",
                0.95,
            ),
            _network_candidate(
                network,
                "product.title",
                _clean_text(network_product.get("title")),
            ),
            _candidate(dom.get("title"), "stable_dom", "#productTitle", 0.82),
        ),
        "brand": _choose(
            "product.brand",
            evidence,
            None,
            _candidate(structured.get("brand"), "structured_data", "jsonld.Product.brand", 0.98),
            _candidate(
                _clean_text(product_state.get("brand")),
                "embedded_state",
                "state.product.brand",
                0.95,
            ),
            _network_candidate(
                network,
                "product.brand",
                _clean_text(network_product.get("brand")),
            ),
            _candidate(dom.get("brand"), "stable_dom", "#bylineInfo", 0.8),
        ),
        "category_path": _choose(
            "product.category_path",
            evidence,
            [],
            _candidate(
                structured.get("category_path"),
                "structured_data",
                "jsonld.Product.category",
                0.95,
            ),
            _candidate(
                _category_path(product_state.get("category_path")),
                "embedded_state",
                "state.product.category_path",
                0.95,
                accept_empty=_explicit_empty_list(product_state, "category_path"),
            ),
            _network_candidate(
                network,
                "product.category_path",
                _category_path(network_product.get("category_path")),
                accept_empty="category_path" in network_product,
            ),
            _candidate(
                dom.get("category_path"),
                "stable_dom",
                "#wayfinding-breadcrumbs_feature_div",
                0.8,
            ),
        ),
        "bullet_points": _choose(
            "product.bullet_points",
            evidence,
            [],
            _candidate(
                structured.get("bullet_points"),
                "structured_data",
                "jsonld.Product.positiveNotes",
                0.95,
            ),
            _candidate(
                _text_list(product_state.get("bullet_points")),
                "embedded_state",
                "state.product.bullet_points",
                0.95,
                accept_empty=_explicit_empty_list(product_state, "bullet_points"),
            ),
            _network_candidate(
                network,
                "product.bullet_points",
                _text_list(network_product.get("bullet_points")),
                accept_empty="bullet_points" in network_product,
            ),
            _candidate(dom.get("bullet_points"), "stable_dom", "#feature-bullets", 0.82),
        ),
        "description": _choose(
            "product.description",
            evidence,
            None,
            _candidate(
                structured.get("description"),
                "structured_data",
                "jsonld.Product.description",
                0.98,
            ),
            _candidate(
                _clean_text(product_state.get("description")),
                "embedded_state",
                "state.product.description",
                0.95,
            ),
            _network_candidate(
                network,
                "product.description",
                _clean_text(network_product.get("description")),
            ),
            _candidate(dom.get("description"), "stable_dom", "#productDescription", 0.8),
        ),
        "technical_details": _choose(
            "product.technical_details",
            evidence,
            {},
            _candidate(
                _string_mapping(product_state.get("technical_details")),
                "embedded_state",
                "state.product.technical_details",
                0.95,
                accept_empty=_explicit_empty_mapping(product_state, "technical_details"),
            ),
            _network_candidate(
                network,
                "product.technical_details",
                _string_mapping(network_product.get("technical_details")),
                accept_empty="technical_details" in network_product,
            ),
            _candidate(
                dom.get("technical_details"),
                "stable_dom",
                "#productDetails_techSpec_section_1",
                0.82,
            ),
        ),
    }

    availability = _choose(
        "commerce.availability_status",
        evidence,
        "unknown",
        _candidate(
            structured.get("availability_status"),
            "structured_data",
            "jsonld.Product.offers.availability",
            0.98,
            _availability_evidence_status(structured.get("availability_status")),
        ),
        _candidate(
            _normalize_availability(commerce_state.get("availability_status")),
            "embedded_state",
            "state.commerce.availability_status",
            0.95,
            _availability_evidence_status(commerce_state.get("availability_status")),
        ),
        _network_candidate(
            network,
            "commerce.availability_status",
            _normalize_availability(network_commerce.get("availability_status")),
            status=_availability_evidence_status(network_commerce.get("availability_status")),
        ),
        _candidate(
            dom.get("availability_status"),
            "stable_dom",
            "#availability",
            0.85,
            _availability_evidence_status(dom.get("availability_status")),
        ),
        _candidate(
            _controlled_availability(document),
            "controlled_text",
            "document.availability_text",
            0.65,
            _availability_evidence_status(_controlled_availability(document)),
        ),
    )

    featured_offer = {
        "seller_id": _choose_offer_field(
            "seller_id", evidence, structured_offer, offer_state, network_offer, network, dom
        ),
        "seller_name": _choose_offer_field(
            "seller_name", evidence, structured_offer, offer_state, network_offer, network, dom
        ),
        "is_buy_box": _choose_offer_field(
            "is_buy_box", evidence, structured_offer, offer_state, network_offer, network, dom
        ),
        "price_amount": _choose_offer_field(
            "price_amount", evidence, structured_offer, offer_state, network_offer, network, dom
        ),
        "list_price_amount": _choose_offer_field(
            "list_price_amount",
            evidence,
            structured_offer,
            offer_state,
            network_offer,
            network,
            dom,
        ),
        "currency": _choose_offer_field(
            "currency", evidence, structured_offer, offer_state, network_offer, network, dom
        ),
        "fulfillment_channel": _choose_offer_field(
            "fulfillment_channel",
            evidence,
            structured_offer,
            offer_state,
            network_offer,
            network,
            dom,
        ),
        "delivery_text": _choose_offer_field(
            "delivery_text", evidence, structured_offer, offer_state, network_offer, network, dom
        ),
        "coupon_text": _choose_offer_field(
            "coupon_text", evidence, structured_offer, offer_state, network_offer, network, dom
        ),
        "promotions": _choose_offer_field(
            "promotions", evidence, structured_offer, offer_state, network_offer, network, dom
        ),
    }
    commerce = {
        "availability_status": availability,
        "rating": _choose(
            "commerce.rating",
            evidence,
            None,
            _candidate(structured.get("rating"), "structured_data", "jsonld.aggregateRating", 0.98),
            _candidate(
                _as_float(commerce_state.get("rating")),
                "embedded_state",
                "state.commerce.rating",
                0.95,
            ),
            _network_candidate(
                network,
                "commerce.rating",
                _as_float(network_commerce.get("rating")),
            ),
            _candidate(dom.get("rating"), "stable_dom", "#acrPopover", 0.82),
        ),
        "review_count": _choose(
            "commerce.review_count",
            evidence,
            None,
            _candidate(
                structured.get("review_count"),
                "structured_data",
                "jsonld.aggregateRating.reviewCount",
                0.98,
            ),
            _candidate(
                _as_int(commerce_state.get("review_count")),
                "embedded_state",
                "state.commerce.review_count",
                0.95,
            ),
            _network_candidate(
                network,
                "commerce.review_count",
                _as_int(network_commerce.get("review_count")),
            ),
            _candidate(dom.get("review_count"), "stable_dom", "#acrCustomerReviewText", 0.82),
        ),
        "featured_offer": featured_offer,
    }

    variants = {
        "parent_asin": _choose(
            "variants.parent_asin",
            evidence,
            None,
            _candidate(
                state_parent,
                "embedded_state",
                "state.parent_asin",
                0.95,
            ),
            _candidate(
                state_variant_parent,
                "embedded_state",
                "state.variants.parent_asin",
                0.95,
            ),
            _network_candidate(
                network,
                "variants.parent_asin",
                network_parent,
            ),
            _candidate(dom_parent, "stable_dom", "#twister[data-parent-asin]", 0.8),
        ),
        "child_asins": _choose(
            "variants.child_asins",
            evidence,
            [],
            _candidate(
                _normalize_asin_list(state_variants.get("child_asins")),
                "embedded_state",
                "state.variants.child_asins",
                0.95,
                accept_empty=_explicit_empty_list(state_variants, "child_asins"),
            ),
            _network_candidate(
                network,
                "variants.child_asins",
                _normalize_asin_list(network_variants.get("child_asins")),
                accept_empty="child_asins" in network_variants,
            ),
            _candidate(dom.get("child_asins"), "stable_dom", "#twister [data-asin]", 0.8),
        ),
        "current_attributes": _choose(
            "variants.current_attributes",
            evidence,
            {},
            _candidate(
                _string_mapping(state_variants.get("current_attributes")),
                "embedded_state",
                "state.variants.current_attributes",
                0.95,
                accept_empty=_explicit_empty_mapping(state_variants, "current_attributes"),
            ),
            _network_candidate(
                network,
                "variants.current_attributes",
                _string_mapping(network_variants.get("current_attributes")),
                accept_empty="current_attributes" in network_variants,
            ),
            _candidate(
                dom.get("current_attributes"),
                "stable_dom",
                "#twister[data-current-attributes]",
                0.78,
            ),
        ),
        "dimensions": _choose(
            "variants.dimensions",
            evidence,
            {},
            _candidate(
                _dimension_mapping(state_variants.get("dimensions")),
                "embedded_state",
                "state.variants.dimensions",
                0.95,
                accept_empty=_explicit_empty_mapping(state_variants, "dimensions"),
            ),
            _network_candidate(
                network,
                "variants.dimensions",
                _dimension_mapping(network_variants.get("dimensions")),
                accept_empty="dimensions" in network_variants,
            ),
            _candidate(
                dom.get("dimensions"),
                "stable_dom",
                "#twister[data-dimensions]",
                0.78,
            ),
        ),
    }

    rankings = _choose(
        "rankings",
        evidence,
        [],
        _candidate(
            _normalize_rankings(state.get("rankings")),
            "embedded_state",
            "state.rankings",
            0.95,
            accept_empty=_explicit_empty_list(state, "rankings"),
        ),
        _network_candidate(
            network,
            "rankings",
            _normalize_rankings(network.get("rankings")),
            accept_empty="rankings" in network,
        ),
        _candidate(
            dom.get("rankings"),
            "controlled_text",
            "#productDetails_detailBullets_sections1 Best Sellers Rank",
            0.72,
        ),
    )

    structured_images = _normalize_media_list(structured.get("images"))
    state_gallery = _normalize_media_list(media_state.get("gallery_images"))
    media = {
        "main_image": _choose(
            "media.main_image",
            evidence,
            None,
            _candidate(
                structured_images[0] if structured_images else None,
                "structured_data",
                "jsonld.Product.image[0]",
                0.98,
            ),
            _candidate(
                _normalize_media_item(media_state.get("main_image")),
                "embedded_state",
                "state.media.main_image",
                0.95,
            ),
            _network_candidate(
                network,
                "media.main_image",
                _normalize_media_item(network_media.get("main_image")),
            ),
            _candidate(dom.get("main_image"), "stable_dom", "#landingImage", 0.85),
        ),
        "gallery_images": _choose(
            "media.gallery_images",
            evidence,
            [],
            _candidate(
                structured_images,
                "structured_data",
                "jsonld.Product.image",
                0.98,
            ),
            _candidate(
                state_gallery,
                "embedded_state",
                "state.media.gallery_images",
                0.95,
                accept_empty=_explicit_empty_list(media_state, "gallery_images"),
            ),
            _network_candidate(
                network,
                "media.gallery_images",
                _normalize_media_list(network_media.get("gallery_images")),
                accept_empty="gallery_images" in network_media,
            ),
            _candidate(dom.get("gallery_images"), "stable_dom", "#altImages img", 0.82),
        ),
    }

    if parent_redirect:
        product = {
            name: _policy_default(
                evidence,
                f"product.{name}",
                default,
                status="missing",
                reason="parent_redirect",
            )
            for name, default in {
                "title": None,
                "brand": None,
                "category_path": [],
                "bullet_points": [],
                "description": None,
                "technical_details": {},
            }.items()
        }
        commerce["availability_status"] = _policy_default(
            evidence,
            "commerce.availability_status",
            "unknown",
            status="missing",
            reason="parent_redirect",
        )
        commerce["rating"] = _policy_default(
            evidence,
            "commerce.rating",
            None,
            status="missing",
            reason="parent_redirect",
        )
        commerce["review_count"] = _policy_default(
            evidence,
            "commerce.review_count",
            None,
            status="missing",
            reason="parent_redirect",
        )
        featured_offer = _suppress_offer(evidence, status="missing", reason="parent_redirect")
        commerce["featured_offer"] = featured_offer
        rankings = _policy_default(
            evidence,
            "rankings",
            [],
            status="missing",
            reason="parent_redirect",
        )
        media = {
            "main_image": _policy_default(
                evidence,
                "media.main_image",
                None,
                status="missing",
                reason="parent_redirect",
            ),
            "gallery_images": _policy_default(
                evidence,
                "media.gallery_images",
                [],
                status="missing",
                reason="parent_redirect",
            ),
        }
    elif availability == "unavailable":
        featured_offer = _suppress_offer(
            evidence,
            status="explicitly_unavailable",
            reason="product_unavailable",
        )
        commerce["featured_offer"] = featured_offer

    if parent_redirect:
        collection_status = "partial_success"
    elif availability == "unavailable":
        collection_status = "unavailable"
    elif any(item["status"] == "missing" for item in evidence.values()):
        collection_status = "partial_success"
    else:
        collection_status = "success"

    capture = {
        "contract_revision": 1,
        "source_platform": "amazon",
        "marketplace_code": "US",
        "requested_asin": requested,
        "resolved_asin": resolved,
        "canonical_url": canonical_amazon_url(requested),
        "captured_at": _normalize_observed_at(observed_at),
        "profile_context": {},
        "collection_status": collection_status,
        "product": product,
        "commerce": commerce,
        "variants": variants,
        "rankings": rankings,
        "media": media,
        "field_evidence": evidence,
        "artifact_refs": [],
    }
    json.dumps(capture, allow_nan=False)
    return capture


def extract_amazon_network_product_data(
    observations: Sequence[Mapping[str, Any]],
    *,
    expected_asin: object,
) -> dict[str, Any]:
    """Return only contract fields anchored to the resolved product identity."""

    expected = normalize_asin(expected_asin)
    merged: dict[str, Any] = {}
    source_paths: list[str] = []
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        payload = observation.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if amazon_network_product_data_asin(payload) != expected:
            continue
        source_path = _network_source_path(observation.get("source_path"))
        for product_object in _network_product_objects(payload):
            if _network_object_asin(product_object) != expected:
                continue
            normalized = _normalize_network_product_object(product_object)
            if not normalized:
                continue
            _merge_network_fields(merged, normalized)
            if source_path not in source_paths:
                source_paths.append(source_path)

    if not merged:
        return {}
    result = {"asin": expected, **merged}
    try:
        serialized = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return {}
    digest = hashlib.sha256(serialized).hexdigest()
    source_path = source_paths[0] if len(source_paths) == 1 else "/page-data"
    result["source_locator"] = f"{source_path}#sha256={digest}"
    return result


def amazon_network_product_data_asin(payload: Mapping[str, Any]) -> str | None:
    """Return the one unambiguous ASIN carried by allowlisted product data."""

    if not isinstance(payload, Mapping):
        return None
    product_objects = tuple(_network_product_objects(payload))
    observed: set[str] = set()
    for product_object in product_objects:
        for raw in _network_direct_identity_values(product_object):
            if raw in (None, ""):
                continue
            try:
                observed.add(normalize_asin(raw))
            except InvalidASINError:
                return None
    if len(observed) != 1:
        return None
    asin = next(iter(observed))
    if not any(
        _network_object_asin(product_object) == asin
        and bool(_normalize_network_product_object(product_object))
        for product_object in product_objects
    ):
        return None
    return asin


def _validated_network_product_data(
    value: Mapping[str, Any] | None,
    *,
    expected_asin: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    try:
        observed_asin = normalize_asin(value.get("asin"))
    except InvalidASINError:
        return {}
    return dict(value) if observed_asin == expected_asin else {}


def _network_product_objects(payload: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    containers = [payload]
    data = _network_field_map(payload).get("data")
    if isinstance(data, Mapping):
        containers.append(data)
    for container in containers:
        yield container
        product = _network_pick(
            _network_field_map(container),
            "product",
            "product_data",
        )
        if isinstance(product, Mapping):
            yield product


def _network_direct_identity_values(value: Mapping[str, Any]) -> Iterable[Any]:
    for raw_key, raw_value in value.items():
        if _network_field_name(raw_key) in {"asin", "product_asin"}:
            yield raw_value
    identity = _network_field_map(value).get("identity")
    if isinstance(identity, Mapping):
        for raw_key, raw_value in identity.items():
            if _network_field_name(raw_key) in {"asin", "product_asin"}:
                yield raw_value


def _network_object_asin(value: Mapping[str, Any]) -> str | None:
    source = _network_field_map(value)
    identity = _network_field_map(source.get("identity"))
    product = _network_field_map(_network_pick(source, "product", "product_data"))
    product_identity = _network_field_map(product.get("identity"))
    observed: set[str] = set()
    for identity_source in (source, identity, product, product_identity):
        for key in ("asin", "product_asin"):
            raw = identity_source.get(key)
            if raw in (None, ""):
                continue
            try:
                observed.add(normalize_asin(raw))
            except InvalidASINError:
                return None
    return next(iter(observed)) if len(observed) == 1 else None


def _normalize_network_product_object(value: Mapping[str, Any]) -> dict[str, Any]:
    source = _network_field_map(value)
    product_source = _network_field_map(_network_pick(source, "product", "product_data")) or source
    commerce_source = _network_field_map(source.get("commerce")) or source
    offer_source = _network_field_map(
        _network_pick(commerce_source, "featured_offer", "buy_box")
    ) or _network_field_map(_network_pick(source, "featured_offer", "buy_box"))
    variants_source = (
        _network_field_map(_network_pick(source, "variants", "variation_data")) or source
    )
    media_source = _network_field_map(source.get("media")) or source

    brand_value = _network_pick(product_source, "brand", "brand_name")
    if isinstance(brand_value, Mapping):
        brand_value = _network_pick(_network_field_map(brand_value), "name", "value")

    category_path_value = _network_pick(
        product_source,
        "category_path",
        "categories",
    )
    bullet_points_value = _network_pick(
        product_source,
        "bullet_points",
        "features",
    )
    technical_details_value = _network_pick(
        product_source,
        "technical_details",
        "specifications",
    )

    product = _compact_network_mapping(
        {
            "title": _network_text(
                _network_pick(product_source, "title", "product_title")
            ),
            "brand": _network_text(brand_value),
            "category_path": _network_collection_value(
                category_path_value,
                _network_text_list(category_path_value),
            ),
            "bullet_points": _network_collection_value(
                bullet_points_value,
                _network_text_list(bullet_points_value),
            ),
            "description": _network_text(
                _network_pick(product_source, "description", "product_description")
            ),
            "technical_details": _network_collection_value(
                technical_details_value,
                _network_string_mapping(technical_details_value),
            ),
        }
    )
    availability = _network_scalar(
        _network_pick(commerce_source, "availability_status", "availability")
    )
    commerce = _compact_network_mapping(
        {
            "availability_status": _normalize_availability(availability),
            "rating": _as_float(
                _network_scalar(
                    _network_pick(commerce_source, "rating", "rating_value", "average_rating")
                )
            ),
            "review_count": _as_int(
                _network_scalar(_network_pick(commerce_source, "review_count", "rating_count"))
            ),
            "featured_offer": _compact_network_mapping(
                {
                    field_name: _network_offer_value(offer_source, field_name)
                    for field_name in _OFFER_FIELD_DEFAULTS
                }
            ),
        }
    )
    child_asins_value = _network_pick(variants_source, "child_asins")
    current_attributes_value = _network_pick(
        variants_source,
        "current_attributes",
        "selected_attributes",
    )
    dimensions_value = _network_pick(
        variants_source,
        "dimensions",
        "variation_dimensions",
    )
    variants = _compact_network_mapping(
        {
            "parent_asin": _optional_asin(_network_pick(variants_source, "parent_asin")),
            "child_asins": _network_collection_value(
                child_asins_value,
                _normalize_asin_list(
                    child_asins_value[:_NETWORK_MAX_LIST_ITEMS]
                    if isinstance(child_asins_value, list)
                    else []
                ),
            ),
            "current_attributes": _network_collection_value(
                current_attributes_value,
                _network_string_mapping(current_attributes_value),
            ),
            "dimensions": _network_collection_value(
                dimensions_value,
                _network_dimension_mapping(dimensions_value),
            ),
        }
    )
    gallery_images_value = _network_pick(media_source, "gallery_images", "images")
    rankings_value = _network_pick(source, "rankings", "best_seller_ranks", "bsr")
    media = _compact_network_mapping(
        {
            "main_image": _normalize_network_media_item(
                _network_pick(media_source, "main_image", "primary_image")
            ),
            "gallery_images": _network_collection_value(
                gallery_images_value,
                _normalize_network_media_list(gallery_images_value),
            ),
        }
    )
    return _compact_network_mapping(
        {
            "product": product,
            "commerce": commerce,
            "variants": variants,
            "rankings": _network_collection_value(
                rankings_value,
                _normalize_network_rankings(rankings_value),
            ),
            "media": media,
        }
    )


def _network_offer_value(source: Mapping[str, Any], field_name: str) -> Any:
    aliases = {
        "seller_id": ("seller_id",),
        "seller_name": ("seller_name",),
        "is_buy_box": ("is_buy_box", "buy_box_winner"),
        "price_amount": ("price_amount", "price_to_pay", "price"),
        "list_price_amount": ("list_price_amount", "list_price"),
        "currency": ("currency", "currency_code"),
        "fulfillment_channel": ("fulfillment_channel", "fulfillment"),
        "delivery_text": ("delivery_text",),
        "coupon_text": ("coupon_text",),
        "promotions": ("promotions", "promotion_messages"),
    }
    raw = _network_pick(source, *aliases[field_name])
    value = _network_scalar(raw)
    if field_name == "promotions":
        return _network_collection_value(raw, _network_text_list(value))
    if field_name in {
        "seller_id",
        "seller_name",
        "currency",
        "fulfillment_channel",
        "delivery_text",
        "coupon_text",
    }:
        if field_name == "delivery_text":
            return _network_delivery_text(value)
        return _network_text(value, max_length=4_000)
    return _normalize_offer_value(field_name, value)


def _network_pick(source: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source:
            return source[key]
    return None


def _network_field_map(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    collisions: set[str] = set()
    for key, item in value.items():
        normalized = _network_field_name(key)
        if normalized in result or normalized in collisions:
            result.pop(normalized, None)
            collisions.add(normalized)
            continue
        result[normalized] = item
    return result


def _network_field_name(value: Any) -> str:
    text = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", str(value))
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text).lower()


def _network_scalar(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _network_pick(
            _network_field_map(value),
            "amount",
            "value",
            "display_value",
            "name",
        )
    return value


def _network_text(value: Any, *, max_length: int = _NETWORK_MAX_TEXT_LENGTH) -> str | None:
    text = _clean_text(value)
    if not text or len(text) > max_length:
        return None
    if re.search(r"\bBearer\s+\S+", text, flags=re.IGNORECASE):
        return None
    if re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text, re.IGNORECASE):
        return None
    if re.search(
        r"\b(?:authorization|cookie|session(?:\s*id)?|token|access\s*key|api\s*key|"
        r"password|secret|email)\s*[:=]\s*\S+",
        text,
        flags=re.IGNORECASE,
    ):
        return None
    return text


def _network_delivery_text(value: Any) -> str | None:
    text = _network_text(value, max_length=4_000)
    if not text:
        return None
    if re.search(
        r"\b(?:deliver(?:y|ing)?|ship(?:ping|s)?)\s+to\b",
        text,
        flags=re.IGNORECASE,
    ):
        return None
    if re.search(
        r"\b\d{1,6}\s+[A-Za-z0-9.' -]+\s(?:street|st|road|rd|avenue|ave|lane|ln|"
        r"drive|dr|boulevard|blvd)\b",
        text,
        flags=re.IGNORECASE,
    ):
        return None
    return text


def _network_text_list(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for raw in values[:_NETWORK_MAX_LIST_ITEMS]:
        text = _network_text(raw, max_length=4_000)
        if text:
            result.append(text)
    return result


def _network_string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, str] = {}
    for raw_key, raw_value in list(value.items())[:_NETWORK_MAX_MAPPING_ITEMS]:
        key = _network_text(raw_key, max_length=200)
        item = _network_text(raw_value, max_length=4_000)
        if key and item and not _network_sensitive_key(key):
            result[key] = item
    return result


def _network_dimension_mapping(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, list[str]] = {}
    for raw_key, raw_values in list(value.items())[:_NETWORK_MAX_MAPPING_ITEMS]:
        key = _network_text(raw_key, max_length=200)
        values = _network_text_list(raw_values)
        if key and values and not _network_sensitive_key(key):
            result[key] = values
    return result


def _network_sensitive_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    words = set(re.findall(r"[a-z0-9]+", _network_field_name(value)))
    return normalized in _NETWORK_SENSITIVE_KEY_MARKERS or bool(
        words & set(_NETWORK_SENSITIVE_KEY_MARKERS)
    )


def _normalize_network_media_item(value: Any) -> dict[str, str] | None:
    item = _normalize_media_item(value)
    if not item:
        return None
    parsed = urlparse(item["url"])
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or not any(
            host == suffix.removeprefix(".") or host.endswith(suffix)
            for suffix in _NETWORK_MEDIA_HOST_SUFFIXES
        )
    ):
        return None
    return {"url": parsed._replace(query="", fragment="").geturl()}


def _normalize_network_media_list(value: Any) -> list[dict[str, str]]:
    values = value if isinstance(value, list) else [value]
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in values[:_NETWORK_MAX_LIST_ITEMS]:
        item = _normalize_network_media_item(raw)
        if item and item["url"] not in seen:
            seen.add(item["url"])
            result.append(item)
    return result


def _normalize_network_rankings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in value[:_NETWORK_MAX_LIST_ITEMS]:
        source = _network_field_map(raw)
        name = _network_text(source.get("category_name"), max_length=500)
        rank = _as_int(source.get("rank"))
        if name and rank is not None:
            result.append(
                {
                    "category_name": name,
                    "category_path": _network_text_list(source.get("category_path"))
                    or [name],
                    "rank": rank,
                }
            )
    return result


def _compact_network_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if item is _NETWORK_EXPLICIT_EMPTY_LIST:
            result[key] = []
        elif item is _NETWORK_EXPLICIT_EMPTY_MAPPING:
            result[key] = {}
        elif _is_present(item):
            result[key] = item
    return result


def _network_collection_value(raw: Any, normalized: Any) -> Any:
    if _is_present(normalized):
        return normalized
    if isinstance(raw, list) and not raw:
        return _NETWORK_EXPLICIT_EMPTY_LIST
    if isinstance(raw, Mapping) and not raw:
        return _NETWORK_EXPLICIT_EMPTY_MAPPING
    return None


def _merge_network_fields(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, Mapping):
            if not value:
                target[key] = {}
                continue
            child = target.setdefault(key, {})
            if isinstance(child, dict):
                _merge_network_fields(child, value)
            continue
        target[key] = value


def _network_source_path(value: Any) -> str:
    path = urlparse(_clean_text(value) or "").path
    return path if path.startswith("/") else "/page-data"


def _parse_document(html: str) -> _DocumentParser:
    parser = _DocumentParser()
    parser.feed(html)
    parser.close()
    return parser


def _iter_nodes(nodes: Iterable[_Node]) -> Iterable[_Node]:
    for node in nodes:
        yield node
        yield from _iter_nodes(node.children)


def _descendants(node: _Node) -> Iterable[_Node]:
    return _iter_nodes(node.children)


def _node_by_id(document: _DocumentParser, element_id: str) -> _Node | None:
    return next(
        (node for node in _iter_nodes(document.roots) if node.attrs.get("id") == element_id),
        None,
    )


def _has_class(node: _Node, class_name: str) -> bool:
    return class_name in node.attrs.get("class", "").split()


def _node_text(node: _Node | None) -> str | None:
    if node is None:
        return None
    chunks = list(node.text_parts)
    for child in node.children:
        child_text = _node_text(child)
        if child_text:
            chunks.append(child_text)
    text = " ".join(" ".join(chunks).split())
    return text or None


def _load_json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _extract_embedded_state(document: _DocumentParser) -> dict[str, Any]:
    node = _node_by_id(document, "amazon-product-state")
    if node is None or node.tag != "script":
        return {}
    value = _load_json(_node_text(node))
    return dict(value) if isinstance(value, Mapping) else {}


def _extract_structured_product(document: _DocumentParser) -> dict[str, Any]:
    product: Mapping[str, Any] | None = None
    for node in _iter_nodes(document.roots):
        if node.tag != "script" or node.attrs.get("type", "").lower() != "application/ld+json":
            continue
        value = _load_json(_node_text(node))
        product = _find_jsonld_product(value)
        if product is not None:
            break
    if product is None:
        return {}

    offer = _first_mapping(product.get("offers"))
    seller = _mapping(offer.get("seller"))
    rating = _mapping(product.get("aggregateRating"))
    brand_value = product.get("brand")
    brand = _mapping(brand_value).get("name") if isinstance(brand_value, Mapping) else brand_value
    images = _normalize_media_list(product.get("image"))
    return {
        "asin": _optional_asin(product.get("sku") or product.get("asin")),
        "title": _clean_text(product.get("name")),
        "brand": _clean_text(brand),
        "description": _clean_text(product.get("description")),
        "category_path": _category_path(product.get("category")),
        "bullet_points": _jsonld_bullets(product.get("positiveNotes")),
        "rating": _as_float(rating.get("ratingValue")),
        "review_count": _as_int(rating.get("reviewCount") or rating.get("ratingCount")),
        "availability_status": _normalize_availability(offer.get("availability")),
        "featured_offer": {
            "seller_name": _clean_text(seller.get("name")),
            "price_amount": _as_float(offer.get("price")),
            "currency": _clean_text(offer.get("priceCurrency")),
        },
        "images": images,
    }


def _find_jsonld_product(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        item_type = value.get("@type")
        types = item_type if isinstance(item_type, list) else [item_type]
        if any(str(candidate).lower() == "product" for candidate in types):
            return value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                result = _find_jsonld_product(item)
                if result is not None:
                    return result
    if isinstance(value, list):
        for item in value:
            result = _find_jsonld_product(item)
            if result is not None:
                return result
    return None


def _extract_dom_values(document: _DocumentParser) -> dict[str, Any]:
    title = _node_text(_node_by_id(document, "productTitle"))
    brand = _clean_brand(_node_text(_node_by_id(document, "bylineInfo")))
    category = _texts_for_descendants(
        _node_by_id(document, "wayfinding-breadcrumbs_feature_div"), tags={"a"}
    )
    bullets = _texts_for_descendants(_node_by_id(document, "feature-bullets"), tags={"li"})
    description = _node_text(_node_by_id(document, "productDescription"))
    technical_details = _table_mapping(_node_by_id(document, "productDetails_techSpec_section_1"))

    price_root = _node_by_id(document, "corePrice_feature_div")
    price_node = next(
        (
            node
            for node in _descendants(price_root)
            if _has_class(node, "a-offscreen")
        ),
        None,
    ) if price_root else None
    price = _as_float(_node_text(price_node))
    list_price = _as_float(_node_text(_node_by_id(document, "priceblock_listprice")))

    rating_node = _node_by_id(document, "acrPopover")
    rating = _as_float(rating_node.attrs.get("title") if rating_node else None)
    review_count = _as_int(_node_text(_node_by_id(document, "acrCustomerReviewText")))
    availability = _normalize_availability(_node_text(_node_by_id(document, "availability")))

    seller = _node_by_id(document, "sellerProfileTriggerId")
    merchant = _node_by_id(document, "merchant-info")
    merchant_text = _node_text(merchant)
    fulfillment = None
    if merchant_text:
        fulfillment = "amazon" if "amazon" in merchant_text.lower() else "merchant"

    twister = _node_by_id(document, "twister")
    child_asins = []
    if twister:
        for node in _descendants(twister):
            asin = _optional_asin(node.attrs.get("data-asin"))
            if asin and asin not in child_asins:
                child_asins.append(asin)

    main_image_node = _node_by_id(document, "landingImage")
    main_image = None
    if main_image_node:
        main_image = _normalize_media_item(
            main_image_node.attrs.get("data-old-hires") or main_image_node.attrs.get("src")
        )
    gallery = []
    gallery_root = _node_by_id(document, "altImages")
    if gallery_root:
        gallery = _normalize_media_list(
            [
                node.attrs.get("data-old-hires") or node.attrs.get("src")
                for node in _descendants(gallery_root)
                if node.tag == "img"
            ]
        )

    return {
        "title": title,
        "brand": brand,
        "category_path": category,
        "bullet_points": bullets,
        "description": description,
        "technical_details": technical_details,
        "availability_status": availability,
        "rating": rating,
        "review_count": review_count,
        "seller_id": seller.attrs.get("data-seller-id") if seller else None,
        "seller_name": _node_text(seller),
        "is_buy_box": _as_bool(merchant.attrs.get("data-buy-box")) if merchant else None,
        "price_amount": price,
        "list_price_amount": list_price,
        "currency": "USD" if price is not None or list_price is not None else None,
        "fulfillment_channel": fulfillment,
        "delivery_text": _node_text(_node_by_id(document, "deliveryBlockMessage")),
        "coupon_text": _node_text(_node_by_id(document, "couponText")),
        "promotions": _texts_for_descendants(
            _node_by_id(document, "promoPriceBlockMessage_feature_div"), tags={"span", "li"}
        ),
        "parent_asin": _optional_asin(twister.attrs.get("data-parent-asin")) if twister else None,
        "child_asins": child_asins,
        "current_attributes": _json_string_mapping(
            twister.attrs.get("data-current-attributes") if twister else None
        ),
        "dimensions": _json_dimension_mapping(
            twister.attrs.get("data-dimensions") if twister else None
        ),
        "rankings": _dom_rankings(document),
        "main_image": main_image,
        "gallery_images": gallery,
    }


def _texts_for_descendants(node: _Node | None, *, tags: set[str]) -> list[str]:
    if node is None:
        return []
    values: list[str] = []
    for child in _descendants(node):
        if child.tag not in tags:
            continue
        text = _node_text(child)
        if text and text not in values:
            values.append(text)
    return values


def _table_mapping(table: _Node | None) -> dict[str, str]:
    if table is None:
        return {}
    result: dict[str, str] = {}
    for row in (node for node in _descendants(table) if node.tag == "tr"):
        heading = next((node for node in _descendants(row) if node.tag == "th"), None)
        value = next((node for node in _descendants(row) if node.tag == "td"), None)
        heading_text = _node_text(heading)
        value_text = _node_text(value)
        if heading_text and value_text:
            result[heading_text] = value_text
    return result


def _dom_rankings(document: _DocumentParser) -> list[dict[str, Any]]:
    table = _node_by_id(document, "productDetails_detailBullets_sections1")
    if table is None:
        return []
    text = _node_text(table) or ""
    result: list[dict[str, Any]] = []
    pattern = re.compile(r"#\s*([\d,]+)\s+in\s+(.+?)(?=#\s*[\d,]+\s+in\s+|$)", re.IGNORECASE)
    for match in pattern.finditer(text):
        name = " ".join(match.group(2).split()).strip(" ;,")
        rank = _as_int(match.group(1))
        if name and rank is not None:
            result.append({"category_name": name, "category_path": [name], "rank": rank})
    return result


def _controlled_availability(document: _DocumentParser) -> str | None:
    for element_id in ("outOfStock", "availabilityInsideBuyBox_feature_div"):
        availability = _normalize_availability(_node_text(_node_by_id(document, element_id)))
        if availability:
            return availability

    page_title = next(
        (_node_text(node) for node in _iter_nodes(document.roots) if node.tag == "title"),
        None,
    )
    normalized_title = (page_title or "").strip().lower()
    if normalized_title in {"amazon.com - page not found", "page not found"}:
        return "unavailable"
    return None


def _raise_if_access_blocked(html: str) -> None:
    lower = html.lower()
    captcha = any(
        marker in lower
        for marker in (
            "/errors/validatecaptcha",
            "id=\"captchacharacters\"",
            "id='captchacharacters'",
            "enter the characters you see below",
        )
    )
    blocked = captcha or any(
        marker in lower
        for marker in (
            "<title>robot check</title>",
            "sorry, we just need to check you",
            "automated access to amazon data",
        )
    )
    if blocked:
        raise AmazonAccessBlockedError(
            "Amazon access was blocked by a CAPTCHA or robot check",
            error_code="captcha_required" if captcha else "access_blocked",
        )


def _choose_offer_field(
    field_name: str,
    evidence: dict[str, dict[str, Any]],
    structured: Mapping[str, Any],
    state: Mapping[str, Any],
    network_offer: Mapping[str, Any],
    network: Mapping[str, Any],
    dom: Mapping[str, Any],
) -> Any:
    default = _OFFER_FIELD_DEFAULTS[field_name]
    return _choose(
        f"commerce.featured_offer.{field_name}",
        evidence,
        list(default) if isinstance(default, list) else default,
        _candidate(
            _normalize_offer_value(field_name, structured.get(field_name)),
            "structured_data",
            f"jsonld.Product.offers.{field_name}",
            0.98,
        ),
        _candidate(
            _normalize_offer_value(field_name, state.get(field_name)),
            "embedded_state",
            f"state.commerce.featured_offer.{field_name}",
            0.95,
            accept_empty=(
                field_name == "promotions" and _explicit_empty_list(state, field_name)
            ),
        ),
        _network_candidate(
            network,
            f"commerce.featured_offer.{field_name}",
            _normalize_offer_value(field_name, network_offer.get(field_name)),
            accept_empty=field_name == "promotions" and field_name in network_offer,
        ),
        _candidate(
            _normalize_offer_value(field_name, dom.get(field_name)),
            "stable_dom",
            f"dom.featured_offer.{field_name}",
            0.8,
        ),
    )


def _network_candidate(
    network: Mapping[str, Any],
    field_path: str,
    value: Any,
    *,
    status: str = "observed",
    accept_empty: bool = False,
) -> _Candidate:
    source_locator = _clean_text(network.get("source_locator"))
    locator = f"{source_locator}:{field_path}" if source_locator else field_path
    return _candidate(
        value,
        "same_origin_response",
        locator,
        0.88,
        status,
        accept_empty=accept_empty,
    )


def _candidate(
    value: Any,
    source_kind: str,
    source_locator: str,
    confidence: float,
    status: str = "observed",
    *,
    accept_empty: bool = False,
) -> _Candidate:
    return _Candidate(value, source_kind, source_locator, confidence, status, accept_empty)


def _choose(
    path: str,
    evidence: dict[str, dict[str, Any]],
    default: Any,
    *candidates: _Candidate,
) -> Any:
    for candidate in candidates:
        if not candidate.accept_empty and not _is_present(candidate.value):
            continue
        evidence[path] = {
            "value": candidate.value,
            "status": candidate.status,
            "source_kind": candidate.source_kind,
            "source_locator": candidate.source_locator,
            "confidence": candidate.confidence,
        }
        return candidate.value
    evidence[path] = {
        "value": default,
        "status": "missing",
        "source_kind": None,
        "source_locator": None,
        "confidence": 0.0,
    }
    return default


def _suppress_offer(
    evidence: dict[str, dict[str, Any]],
    *,
    status: str,
    reason: str,
) -> dict[str, Any]:
    offer: dict[str, Any] = {}
    for field_name, raw_default in _OFFER_FIELD_DEFAULTS.items():
        default = list(raw_default) if isinstance(raw_default, list) else raw_default
        path = f"commerce.featured_offer.{field_name}"
        offer[field_name] = _policy_default(
            evidence,
            path,
            default,
            status=status,
            reason=reason,
        )
    return offer


def _policy_default(
    evidence: dict[str, dict[str, Any]],
    path: str,
    default: Any,
    *,
    status: str,
    reason: str,
) -> Any:
    if isinstance(default, list):
        default = list(default)
    elif isinstance(default, dict):
        default = dict(default)
    evidence[path] = {
        "value": default,
        "status": status,
        "source_kind": "identity_policy" if reason == "parent_redirect" else "availability_policy",
        "source_locator": reason,
        "confidence": 1.0,
    }
    return default


def _explicit_empty_list(source: Mapping[str, Any], field_name: str) -> bool:
    return field_name in source and isinstance(source.get(field_name), list) and not source[field_name]


def _explicit_empty_mapping(source: Mapping[str, Any], field_name: str) -> bool:
    return (
        field_name in source
        and isinstance(source.get(field_name), Mapping)
        and not source[field_name]
    )


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, list):
        return next((dict(item) for item in value if isinstance(item, Mapping)), {})
    return {}


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, (str, int, float)):
        return None
    text = " ".join(str(value).split())
    return text or None


def _clean_brand(value: str | None) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    text = re.sub(r"^brand\s*:\s*", "", text, flags=re.IGNORECASE)
    visit_match = re.fullmatch(r"Visit the (.+) Store", text, flags=re.IGNORECASE)
    return visit_match.group(1) if visit_match else text


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).replace(",", "")
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def _category_path(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"\s*>\s*", value) if part.strip()]
    if isinstance(value, list):
        return [text for item in value if (text := _clean_text(item))]
    return []


def _text_list(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [text for item in values if (text := _clean_text(item))]


def _normalize_offer_value(field_name: str, value: Any) -> Any:
    if field_name in {"seller_id", "seller_name", "delivery_text", "coupon_text"}:
        return _clean_text(value)
    if field_name == "is_buy_box":
        return _as_bool(value)
    if field_name in {"price_amount", "list_price_amount"}:
        return _as_float(value)
    if field_name == "currency":
        text = _clean_text(value)
        return text.upper() if text else None
    if field_name == "fulfillment_channel":
        return _normalize_fulfillment(value)
    if field_name == "promotions":
        return _text_list(value)
    return value


def _normalize_fulfillment(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    normalized = text.lower().replace("-", "_").replace(" ", "_")
    if normalized in {"amazon", "fba", "fulfilled_by_amazon"}:
        return "amazon"
    if normalized in {"merchant", "fbm", "fulfilled_by_merchant"}:
        return "merchant"
    if normalized == "unknown":
        return "unknown"
    return None


def _jsonld_bullets(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        value = value.get("itemListElement")
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        raw = item.get("name") if isinstance(item, Mapping) else item
        text = _clean_text(raw)
        if text:
            result.append(text)
    return result


def _normalize_availability(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    lowered = text.lower().replace("-", "_").replace(" ", "_")
    if any(marker in lowered for marker in ("unavailable", "discontinued", "not_found")):
        return "unavailable"
    if any(
        marker in lowered
        for marker in ("not_in_stock", "outofstock", "out_of_stock", "temporarily_out_of_stock")
    ):
        return "out_of_stock"
    if "instock" in lowered or "in_stock" in lowered:
        return "in_stock"
    if lowered == "unknown":
        return "unknown"
    return None


def _availability_evidence_status(value: Any) -> str:
    return "explicitly_unavailable" if _normalize_availability(value) == "unavailable" else "observed"


def _normalize_media_item(value: Any) -> dict[str, str] | None:
    if isinstance(value, Mapping):
        value = value.get("url") or value.get("contentUrl")
    text = _clean_text(value)
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return {"url": text}


def _normalize_media_list(value: Any) -> list[dict[str, str]]:
    values: Sequence[Any]
    if isinstance(value, (str, Mapping)):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in values:
        item = _normalize_media_item(raw)
        if item and item["url"] not in seen:
            seen.add(item["url"])
            result.append(item)
    return result


def _optional_asin(value: Any) -> str | None:
    try:
        return normalize_asin(value)
    except InvalidASINError:
        return None


def _first_asin(*values: Any) -> str | None:
    return next((asin for value in values if (asin := _optional_asin(value))), None)


def _normalize_asin_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for raw in value:
        asin = _optional_asin(raw)
        if asin and asin not in result:
            result.append(asin)
    return result


def _string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _clean_text(raw_key)
        item = _clean_text(raw_value)
        if key and item:
            result[key] = item
    return result


def _dimension_mapping(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, list[str]] = {}
    for raw_key, raw_values in value.items():
        key = _clean_text(raw_key)
        if not key:
            continue
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        normalized = [text for item in values if (text := _clean_text(item))]
        if normalized:
            result[key] = normalized
    return result


def _json_string_mapping(value: str | None) -> dict[str, str]:
    return _string_mapping(_load_json(value))


def _json_dimension_mapping(value: str | None) -> dict[str, list[str]]:
    return _dimension_mapping(_load_json(value))


def _normalize_rankings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        name = _clean_text(raw.get("category_name"))
        rank = _as_int(raw.get("rank"))
        if not name or rank is None:
            continue
        result.append(
            {
                "category_name": name,
                "category_path": _category_path(raw.get("category_path")) or [name],
                "rank": rank,
            }
        )
    return result


def _normalize_observed_at(value: datetime | str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise AmazonProductExtractionError("observed_at must be an ISO-8601 string or datetime")
