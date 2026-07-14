from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from automation_business_scaffold.capabilities.browser.amazon.product_page import (
    AmazonAccessBlockedError,
    AmazonIdentityMismatchError,
    InvalidASINError,
    UnsupportedMarketplaceError,
    canonical_amazon_url,
    extract_amazon_product_capture,
    extract_asin_from_url,
    normalize_asin,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "amazon"
OBSERVED_AT = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" b0child001 ", "B0CHILD001"),
        ("B0PARENT01", "B0PARENT01"),
    ],
)
def test_normalize_asin_trims_and_uppercases(raw: str, expected: str) -> None:
    assert normalize_asin(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "short", "B0-INVALID", "B0TOOLONG001"])
def test_normalize_asin_rejects_invalid_values(raw: object) -> None:
    with pytest.raises(InvalidASINError) as error:
        normalize_asin(raw)

    assert error.value.error_code == "invalid_asin"


def test_canonical_url_contains_only_the_normalized_asin() -> None:
    assert canonical_amazon_url(" b0child001 ") == "https://www.amazon.com/dp/B0CHILD001"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.amazon.com/dp/B0CHILD001?tag=affiliate-20",
        "https://amazon.com/gp/product/B0CHILD001/ref=something",
        "https://smile.amazon.com/Example-Product/dp/B0CHILD001#reviews",
    ],
)
def test_extract_asin_from_supported_amazon_us_urls(url: str) -> None:
    assert extract_asin_from_url(url) == "B0CHILD001"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.amazon.co.uk/dp/B0CHILD001",
        "https://amazon.com.evil.example/dp/B0CHILD001",
    ],
)
def test_extract_asin_rejects_non_us_marketplaces(url: str) -> None:
    with pytest.raises(UnsupportedMarketplaceError) as error:
        extract_asin_from_url(url)

    assert error.value.error_code == "unsupported_marketplace"


def test_extract_full_capture_uses_layered_precedence_and_all_v1_fields() -> None:
    capture = extract_amazon_product_capture(
        _fixture("product_detail_child.html"),
        requested_asin="B0CHILD001",
        resolved_url="https://www.amazon.com/Example-Product/dp/B0CHILD001?ref_=redirect",
        observed_at=OBSERVED_AT,
    )

    assert capture["contract_revision"] == 1
    assert capture["source_platform"] == "amazon"
    assert capture["marketplace_code"] == "US"
    assert capture["requested_asin"] == "B0CHILD001"
    assert capture["resolved_asin"] == "B0CHILD001"
    assert capture["canonical_url"] == "https://www.amazon.com/dp/B0CHILD001"
    assert capture["captured_at"] == "2026-07-14T08:00:00Z"
    assert capture["profile_context"] == {}
    assert capture["collection_status"] == "success"

    assert capture["product"] == {
        "title": "Structured product title",
        "brand": "Structured Brand",
        "category_path": ["Home & Kitchen", "Lighting", "Table Lamps"],
        "bullet_points": ["Dimmable warm light", "Solid oak base"],
        "description": "Structured product description.",
        "technical_details": {
            "Material": "Oak",
            "Product Dimensions": "10 x 10 x 18 inches",
        },
    }
    assert capture["commerce"] == {
        "availability_status": "in_stock",
        "rating": 4.7,
        "review_count": 1234,
        "featured_offer": {
            "seller_id": "SELLER123",
            "seller_name": "Structured Seller",
            "is_buy_box": True,
            "price_amount": 29.99,
            "list_price_amount": 39.99,
            "currency": "USD",
            "fulfillment_channel": "amazon",
            "delivery_text": "FREE delivery Friday, July 17",
            "coupon_text": "Save 10% with coupon",
            "promotions": ["Buy 2, save 5%"],
        },
    }
    assert capture["variants"] == {
        "parent_asin": "B0PARENT01",
        "child_asins": ["B0CHILD001", "B0CHILD002"],
        "current_attributes": {"Color": "Blue", "Size": "Large"},
        "dimensions": {
            "Color": ["Blue", "Red"],
            "Size": ["Large", "Small"],
        },
    }
    assert capture["rankings"] == [
        {
            "category_name": "Table Lamps",
            "category_path": ["Home & Kitchen", "Lighting", "Table Lamps"],
            "rank": 7,
        },
        {
            "category_name": "Home & Kitchen",
            "category_path": ["Home & Kitchen"],
            "rank": 321,
        },
    ]
    assert capture["media"] == {
        "main_image": {"url": "https://images.example.test/structured-main.jpg"},
        "gallery_images": [
            {"url": "https://images.example.test/structured-main.jpg"},
            {"url": "https://images.example.test/structured-gallery-1.jpg"},
            {"url": "https://images.example.test/structured-gallery-2.jpg"},
        ],
    }
    assert capture["artifact_refs"] == []

    evidence = capture["field_evidence"]
    assert evidence["product.title"]["source_kind"] == "structured_data"
    assert evidence["product.bullet_points"]["source_kind"] == "embedded_state"
    assert evidence["commerce.featured_offer.price_amount"]["source_kind"] == (
        "structured_data"
    )
    assert evidence["commerce.featured_offer.coupon_text"]["source_kind"] == (
        "embedded_state"
    )
    assert all(
        set(item) == {"value", "status", "source_kind", "source_locator", "confidence"}
        for item in evidence.values()
    )

    serialized = json.dumps(capture, sort_keys=True)
    assert "secret-cookie-must-not-leak" not in serialized
    assert "secret-token-must-not-leak" not in serialized
    assert "secret-workspace-must-not-leak" not in serialized


def test_dom_is_used_when_structured_layers_are_absent() -> None:
    html = _fixture("product_detail_child.html")
    html = html.replace(
        '<script type="application/ld+json">', '<script type="application/ignored+json">'
    ).replace(
        '<script id="amazon-product-state" type="application/json">',
        '<script id="ignored-state" type="application/json">',
    )

    capture = extract_amazon_product_capture(
        html,
        requested_asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
        observed_at="2026-07-14T08:00:00Z",
    )

    assert capture["product"]["title"] == "DOM product title"
    assert capture["product"]["bullet_points"] == ["DOM bullet one", "DOM bullet two"]
    assert capture["commerce"]["featured_offer"]["price_amount"] == 32.5
    assert capture["commerce"]["featured_offer"]["seller_id"] == "DOMSELLER"
    assert capture["variants"]["parent_asin"] == "B0PARENT01"
    assert capture["media"]["main_image"] == {
        "url": "https://images.example.test/dom-main.jpg"
    }
    assert capture["field_evidence"]["product.title"]["source_kind"] == "stable_dom"
    assert capture["field_evidence"]["variants.parent_asin"]["source_kind"] == "stable_dom"


def test_embedded_state_scalar_values_are_normalized() -> None:
    html = """
    <html><body>
      <script id="amazon-product-state" type="application/json">
        {
          "asin": "B0CHILD001",
          "product": {
            "title": "  State   title  ",
            "category_path": [" Home ", " Lamps "],
            "bullet_points": [" First  point ", "Second point"],
            "technical_details": {" Material ": " Oak "}
          },
          "commerce": {
            "availability_status": "OutOfStock",
            "rating": "4.6 out of 5",
            "review_count": "1,001 ratings",
            "featured_offer": {
              "is_buy_box": "false",
              "price_amount": "$19.50",
              "currency": " usd ",
              "fulfillment_channel": "FBA",
              "promotions": "Save $2"
            }
          }
        }
      </script>
    </body></html>
    """

    capture = extract_amazon_product_capture(
        html,
        requested_asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
        observed_at=OBSERVED_AT,
    )

    assert capture["product"]["title"] == "State title"
    assert capture["product"]["category_path"] == ["Home", "Lamps"]
    assert capture["product"]["bullet_points"] == ["First point", "Second point"]
    assert capture["product"]["technical_details"] == {"Material": "Oak"}
    assert capture["commerce"]["availability_status"] == "out_of_stock"
    assert capture["commerce"]["rating"] == 4.6
    assert capture["commerce"]["review_count"] == 1001
    assert capture["commerce"]["featured_offer"]["is_buy_box"] is False
    assert capture["commerce"]["featured_offer"]["price_amount"] == 19.5
    assert capture["commerce"]["featured_offer"]["currency"] == "USD"
    assert capture["commerce"]["featured_offer"]["fulfillment_channel"] == "amazon"
    assert capture["commerce"]["featured_offer"]["promotions"] == ["Save $2"]


def test_offerless_available_page_is_partial_without_inventing_buy_box_or_price() -> None:
    html = """
    <html><body>
      <script id="amazon-product-state" type="application/json">
        {
          "asin": "B0CHILD001",
          "product": {"title": "Offerless product"},
          "commerce": {"availability_status": "out_of_stock"}
        }
      </script>
    </body></html>
    """

    capture = extract_amazon_product_capture(
        html,
        requested_asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
        observed_at=OBSERVED_AT,
    )

    assert capture["collection_status"] == "partial_success"
    assert capture["commerce"]["featured_offer"]["price_amount"] is None
    assert capture["commerce"]["featured_offer"]["is_buy_box"] is None
    assert capture["field_evidence"]["commerce.featured_offer.price_amount"]["status"] == (
        "missing"
    )


def test_not_in_stock_is_never_misclassified_as_in_stock() -> None:
    html = """
    <html><body>
      <script id="amazon-product-state" type="application/json">
        {"asin": "B0CHILD001", "product": {"title": "Out of stock product"}}
      </script>
      <div id="availability"><span>This item is not in stock.</span></div>
    </body></html>
    """

    capture = extract_amazon_product_capture(
        html,
        requested_asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
        observed_at=OBSERVED_AT,
    )

    assert capture["commerce"]["availability_status"] == "out_of_stock"
    assert capture["field_evidence"]["commerce.availability_status"]["source_kind"] == (
        "stable_dom"
    )


def test_recommendation_stock_text_does_not_set_current_product_availability() -> None:
    html = """
    <html><body>
      <script id="amazon-product-state" type="application/json">
        {"asin": "B0CHILD001", "product": {"title": "Product with no stock signal"}}
      </script>
      <aside>
        Recommended accessory is in stock and ships today; another accessory is currently
        unavailable.
      </aside>
    </body></html>
    """

    capture = extract_amazon_product_capture(
        html,
        requested_asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
        observed_at=OBSERVED_AT,
    )

    assert capture["commerce"]["availability_status"] == "unknown"
    assert capture["field_evidence"]["commerce.availability_status"]["status"] == "missing"


def test_parent_redirect_to_child_is_partial_and_does_not_expose_child_offer() -> None:
    capture = extract_amazon_product_capture(
        _fixture("product_detail_child.html"),
        requested_asin="B0PARENT01",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
        observed_at=OBSERVED_AT,
    )

    assert capture["collection_status"] == "partial_success"
    assert capture["requested_asin"] == "B0PARENT01"
    assert capture["resolved_asin"] == "B0CHILD001"
    assert capture["canonical_url"] == "https://www.amazon.com/dp/B0PARENT01"
    assert capture["variants"]["parent_asin"] == "B0PARENT01"
    assert capture["product"]["title"] is None
    assert capture["commerce"]["rating"] is None
    assert capture["rankings"] == []
    assert capture["media"] == {"main_image": None, "gallery_images": []}
    assert capture["field_evidence"]["product.title"]["status"] == "missing"
    assert capture["field_evidence"]["media.main_image"]["source_kind"] == "identity_policy"
    assert capture["commerce"]["featured_offer"] == {
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
    assert (
        capture["field_evidence"]["commerce.featured_offer.price_amount"]["status"]
        == "missing"
    )


def test_unrelated_resolved_asin_is_rejected() -> None:
    with pytest.raises(AmazonIdentityMismatchError) as error:
        extract_amazon_product_capture(
            _fixture("product_detail_child.html"),
            requested_asin="B0OTHER001",
            resolved_url="https://www.amazon.com/dp/B0CHILD001",
            observed_at=OBSERVED_AT,
        )

    assert error.value.error_code == "identity_mismatch"


def test_jsonld_identity_must_match_the_resolved_url() -> None:
    html = _fixture("product_detail_child.html").replace(
        '<script id="amazon-product-state" type="application/json">',
        '<script id="ignored-state" type="application/json">',
    ).replace('"sku": "B0CHILD001"', '"sku": "B0OTHER001"')

    with pytest.raises(AmazonIdentityMismatchError) as error:
        extract_amazon_product_capture(
            html,
            requested_asin="B0CHILD001",
            resolved_url="https://www.amazon.com/dp/B0CHILD001",
            observed_at=OBSERVED_AT,
        )

    assert error.value.error_code == "identity_mismatch"


def test_explicit_empty_embedded_collections_remain_observed() -> None:
    html = """
    <html><body>
      <script id="amazon-product-state" type="application/json">
        {
          "asin": "B0CHILD001",
          "product": {
            "title": "Product without optional collections",
            "bullet_points": [],
            "technical_details": {}
          },
          "commerce": {
            "availability_status": "in_stock",
            "featured_offer": {"promotions": []}
          },
          "variants": {
            "child_asins": [],
            "current_attributes": {},
            "dimensions": {}
          },
          "rankings": [],
          "media": {"gallery_images": []}
        }
      </script>
    </body></html>
    """

    capture = extract_amazon_product_capture(
        html,
        requested_asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
        observed_at=OBSERVED_AT,
    )

    observed_empty_paths = {
        "product.bullet_points",
        "product.technical_details",
        "commerce.featured_offer.promotions",
        "variants.child_asins",
        "variants.current_attributes",
        "variants.dimensions",
        "rankings",
        "media.gallery_images",
    }
    assert all(
        capture["field_evidence"][path]["status"] == "observed"
        for path in observed_empty_paths
    )


def test_explicitly_unavailable_page_returns_a_persistable_terminal_capture() -> None:
    capture = extract_amazon_product_capture(
        _fixture("product_detail_unavailable.html"),
        requested_asin="B0UNAVL001",
        resolved_url="https://www.amazon.com/dp/B0UNAVL001",
        observed_at=OBSERVED_AT,
    )

    assert capture["collection_status"] == "unavailable"
    assert capture["commerce"]["availability_status"] == "unavailable"
    assert capture["field_evidence"]["commerce.availability_status"]["status"] == (
        "explicitly_unavailable"
    )
    assert capture["commerce"]["featured_offer"]["price_amount"] is None
    assert (
        capture["field_evidence"]["commerce.featured_offer.price_amount"]["status"]
        == "explicitly_unavailable"
    )
    json.dumps(capture)


def test_robot_check_with_captcha_raises_typed_blocked_error() -> None:
    with pytest.raises(AmazonAccessBlockedError) as error:
        extract_amazon_product_capture(
            _fixture("product_detail_blocked.html"),
            requested_asin="B0BLOCK001",
            resolved_url="https://www.amazon.com/dp/B0BLOCK001",
            observed_at=OBSERVED_AT,
        )

    assert error.value.error_code == "captcha_required"


def test_robot_check_redirect_without_asin_is_still_classified_as_blocked() -> None:
    with pytest.raises(AmazonAccessBlockedError) as error:
        extract_amazon_product_capture(
            _fixture("product_detail_blocked.html"),
            requested_asin="B0BLOCK001",
            resolved_url="https://www.amazon.com/errors/validateCaptcha",
            observed_at=OBSERVED_AT,
        )

    assert error.value.error_code == "captcha_required"


def test_robot_check_without_captcha_uses_access_blocked_error_code() -> None:
    with pytest.raises(AmazonAccessBlockedError) as error:
        extract_amazon_product_capture(
            "<html><title>Robot Check</title><body>Sorry, we just need to check you.</body></html>",
            requested_asin="B0BLOCK001",
            resolved_url="https://www.amazon.com/dp/B0BLOCK001",
            observed_at=OBSERVED_AT,
        )

    assert error.value.error_code == "access_blocked"


def test_plain_product_content_containing_captcha_is_not_treated_as_blocked() -> None:
    html = """
    <html><body>
      <script id="amazon-product-state" type="application/json">
        {
          "asin": "B0CHILD001",
          "product": {
            "title": "CAPTCHA reference card",
            "description": "A printed guide explaining the word captcha."
          },
          "commerce": {"availability_status": "in_stock"}
        }
      </script>
    </body></html>
    """

    capture = extract_amazon_product_capture(
        html,
        requested_asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
        observed_at=OBSERVED_AT,
    )

    assert capture["product"]["title"] == "CAPTCHA reference card"
