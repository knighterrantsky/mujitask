import json

import pytest

from automation_business_scaffold.capabilities.browser.tiktok.product_page import (
    TikTokProductExtractionError,
    TikTokProductUnavailableError,
    extract_tiktok_product_from_html,
)


def _html(product_info):
    data = {"loaderData": {"page": {"page_config": {"components_map": [{
        "component_name": "product_info",
        "component_data": {"product_info": product_info},
    }]}}}}
    return '<script id="__MODERN_ROUTER_DATA__">' + json.dumps(data) + '</script>'


@pytest.fixture
def flat_product():
    return {
        "product_id": "1729449578219344482",
        "title": "Inflatable Pool",
        "sold_count": "197568",
        "images": [{"url_list": ["https://example.com/main.webp"]}],
        "price": {"min_sku_price": "51.99", "real_price": "$51.99 - 64.99",
                  "currency": "USD", "currency_symbol": "$"},
        "seller": {"name": "Pool Shop", "rating": "4.6"},
        "review": {"product_rating": 4.7, "review_count": "12001"},
        "sale_props": [{"prop_name": "Color", "sale_prop_values": [{
            "prop_value": "Pink", "image": {"url_list": ["https://example.com/pink.webp"]},
        }]}],
        "skus": [{"sku_id": "1731225878652228194", "stock": 1266,
                  "sku_sale_props": [{"prop_name": "Color", "prop_value": "Pink"}],
                  "price": {"sale_price_decimal": "64.99", "sale_price_format": "64.99",
                            "currency_symbol": "$", "currency_name": "USD"}}],
    }


def test_flat_product_page_preserves_product_media_and_sku_facts(flat_product):
    product = extract_tiktok_product_from_html(
        _html(flat_product), source_url="https://www.tiktok.com/shop/pdp/1729449578219344482"
    )
    assert product.product_id == "1729449578219344482"
    assert product.title == "Inflatable Pool"
    assert product.main_image_url == "https://example.com/main.webp"
    assert (product.price_amount, product.price_currency, product.price_text) == ("51.99", "USD", "$51.99")
    assert product.sales_count == 197568
    assert product.shop_name == "Pool Shop"
    assert (product.rating_score, product.review_count) == (4.7, 12001)
    assert product.sku_images[0]["source_url"] == "https://example.com/pink.webp"
    assert len(product.skus) == 1
    assert product.skus[0]["price_amount"] == "64.99"
    assert product.skus[0]["price_text"] == "$64.99"
    assert product.skus[0]["spec_name"] == "Color: Pink"


def test_legacy_product_page_remains_supported():
    info = {
        "product_model": {"product_id": "123", "name": "Old Product", "sold_count": 12,
                          "images": [{"url_list": ["https://example.com/old.webp"]}]},
        "promotion_model": {"promotion_product_price": {"min_price": {
            "sale_price_decimal": "9.99", "currency_name": "USD", "currency_symbol": "$",
        }}},
        "seller_model": {"shop_name": "Old Shop"},
    }
    product = extract_tiktok_product_from_html(_html(info), source_url="https://www.tiktok.com/shop/pdp/123")
    assert (product.product_id, product.title, product.price_text, product.shop_name) == (
        "123", "Old Product", "$9.99", "Old Shop"
    )


@pytest.mark.parametrize("missing", ["product_id", "title", "images", "price"])
def test_flat_product_does_not_invent_missing_required_fields(flat_product, missing):
    flat_product.pop(missing)
    if missing == "images":
        flat_product.pop("sale_props")
    with pytest.raises(TikTokProductExtractionError):
        extract_tiktok_product_from_html(_html(flat_product), source_url="https://www.tiktok.com/shop/pdp/123")


def test_removed_product_without_price_is_explicitly_unavailable(flat_product):
    flat_product.pop("price")
    flat_product["unavailable_info"] = {"reason": 4, "text": "This item has been removed"}
    with pytest.raises(TikTokProductUnavailableError, match="removed"):
        extract_tiktok_product_from_html(_html(flat_product), source_url="https://www.tiktok.com/shop/pdp/123")
