from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from automation_business_scaffold.models import TikTokProductRecord


def validate_tiktok_product_url(product_url: str) -> None:
    normalized_url = product_url.strip()
    if not normalized_url:
        raise ValueError("TikTok product url is required")

    parsed = urlparse(normalized_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("TikTok product url must start with http or https")
    if "tiktok.com" not in parsed.netloc:
        raise ValueError("TikTok product url must point to a tiktok.com domain")
    if "/pdp/" not in parsed.path and "/product/" not in parsed.path:
        raise ValueError("TikTok product url must be a TikTok Shop product page")


def validate_tiktok_product_record(
    product: TikTokProductRecord,
    *,
    require_local_image: bool = False,
) -> None:
    if not product.product_id.strip():
        raise ValueError("TikTok product id is required")
    if not product.title.strip():
        raise ValueError("TikTok product title is required")
    if not product.main_image_url.strip():
        raise ValueError("TikTok product main image is required")
    if not product.price_amount.strip():
        raise ValueError("TikTok product price is required")
    if product.sales_count < 0:
        raise ValueError("TikTok product sales_count must be zero or greater")
    if require_local_image:
        if not product.main_image_local_path.strip():
            raise ValueError("TikTok product local main image path is required")
        if not product.main_image_file_name.strip():
            raise ValueError("TikTok product local main image file_name is required")
        if not Path(product.main_image_local_path).exists():
            raise ValueError("TikTok product local main image file does not exist")
