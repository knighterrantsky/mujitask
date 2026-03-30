from __future__ import annotations

import json
from pathlib import Path

from automation_business_scaffold.flows import (
    build_feishu_bitable_record,
    download_tiktok_product_main_image,
    extract_tiktok_product_from_html,
)
from automation_business_scaffold.validators import validate_tiktok_product_url


SAMPLE_ROUTER_DATA = {
    "loaderData": {
        "(region)/pdp/(product_name_slug$)/(product_id)/page": {
            "page_config": {
                "components_map": [
                    {"component_type": "other", "component_name": "other"},
                    {
                        "component_type": "product_info",
                        "component_name": "product_info",
                        "component_data": {
                            "product_info": {
                                "product_model": {
                                    "product_id": "1729732615040962895",
                                    "sold_count": "94151",
                                    "name": "Sample TikTok Product",
                                    "images": [
                                        {
                                            "height": 1400,
                                            "width": 1400,
                                            "uri": "tos-sample/image-1",
                                            "url_list": [
                                                "https://example.com/main-image.webp",
                                            ],
                                        }
                                    ],
                                },
                                "promotion_model": {
                                    "promotion_product_price": {
                                        "min_price": {
                                            "currency_name": "USD",
                                            "currency_symbol": "$",
                                            "sale_price_decimal": "24.99",
                                            "sale_price_format": "24.99",
                                            "origin_price_decimal": "49.99",
                                            "discount_format": "50%",
                                        }
                                    }
                                },
                                "seller_model": {
                                    "shop_name": "Sample Shop",
                                },
                            },
                            "shop_info": {
                                "shop_name": "Sample Shop",
                                "shop_link": "https://shop.tiktok.com/us/store/sample-shop/123",
                                "sold_count": 246407,
                                "format_sold_count": "246.4K",
                            },
                        },
                    },
                ]
            }
        }
    },
    "errors": None,
}

SAMPLE_HTML = (
    "<html><body>"
    '<script id="__MODERN_ROUTER_DATA__" type="application/json">'
    f"{json.dumps(SAMPLE_ROUTER_DATA, ensure_ascii=True)}"
    "</script>"
    "</body></html>"
)


class _FakeImageResponse:
    def __init__(self, content: bytes, content_type: str = "image/webp") -> None:
        self.content = content
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self) -> None:
        return None


class _FakeImageSession:
    def __init__(self, content: bytes, content_type: str = "image/webp") -> None:
        self.response = _FakeImageResponse(content=content, content_type=content_type)

    def get(self, *_args, **_kwargs) -> _FakeImageResponse:
        return self.response

    def close(self) -> None:
        return None


def test_extract_tiktok_product_from_html_returns_expected_fields():
    product = extract_tiktok_product_from_html(
        SAMPLE_HTML,
        source_url="https://shop.tiktok.com/view/product/1729732615040962895",
        resolved_url=(
            "https://shop.tiktok.com/us/pdp/sample-tiktok-product/1729732615040962895"
        ),
    )

    assert product.product_id == "1729732615040962895"
    assert product.title == "Sample TikTok Product"
    assert product.main_image_url == "https://example.com/main-image.webp"
    assert product.price_amount == "24.99"
    assert product.price_currency == "USD"
    assert product.price_text == "$24.99"
    assert product.sales_count == 94151
    assert product.shop_name == "Sample Shop"
    assert product.shop_url == "https://shop.tiktok.com/us/store/sample-shop/123"


def test_download_tiktok_product_main_image_stores_local_file(tmp_path):
    product = extract_tiktok_product_from_html(
        SAMPLE_HTML,
        source_url="https://shop.tiktok.com/view/product/1729732615040962895",
    )
    downloaded = download_tiktok_product_main_image(
        product,
        download_dir=str(tmp_path),
        session=_FakeImageSession(content=b"fake-webp-bytes"),
    )

    assert downloaded.main_image_local_path == str(tmp_path / "1729732615040962895-main-image.webp")
    assert downloaded.main_image_file_name == "1729732615040962895-main-image.webp"
    assert downloaded.main_image_mime_type == "image/webp"
    assert Path(downloaded.main_image_local_path).read_bytes() == b"fake-webp-bytes"


def test_build_feishu_bitable_record_uses_local_file_for_main_image(tmp_path):
    product = extract_tiktok_product_from_html(
        SAMPLE_HTML,
        source_url="https://shop.tiktok.com/view/product/1729732615040962895",
    )
    downloaded = download_tiktok_product_main_image(
        product,
        download_dir=str(tmp_path),
        session=_FakeImageSession(content=b"fake-webp-bytes"),
    )

    record = build_feishu_bitable_record(downloaded)

    assert record["logical_fields"]["title"] == "Sample TikTok Product"
    assert record["logical_fields"]["main_image_local_path"].endswith(
        "1729732615040962895-main-image.webp"
    )
    assert record["fields"] == {
        "商品主图": {
            "type": "local_file",
            "path": str(tmp_path / "1729732615040962895-main-image.webp"),
            "file_name": "1729732615040962895-main-image.webp",
            "mime_type": "image/webp",
            "source_url": "https://example.com/main-image.webp",
        },
        "商品价格": "$24.99",
        "销量": 94151,
        "店铺名称": "Sample Shop",
    }


def test_validate_tiktok_product_url_accepts_expected_product_links():
    validate_tiktok_product_url("https://shop.tiktok.com/view/product/1729732615040962895")
    validate_tiktok_product_url(
        "https://shop.tiktok.com/us/pdp/sample-tiktok-product/1729732615040962895"
    )
