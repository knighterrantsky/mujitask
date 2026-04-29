from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class TikTokProductRecord:
    source_url: str
    resolved_url: str
    normalized_url: str
    product_id: str
    title: str
    holiday: str
    main_image_url: str
    price_amount: str
    price_currency: str
    price_text: str
    sales_count: int
    shop_name: str
    shop_url: str
    gallery_images: list[dict[str, Any]] = field(default_factory=list)
    sku_images: list[dict[str, Any]] = field(default_factory=list)
    skus: list[dict[str, Any]] = field(default_factory=list)
    sku_options: list[dict[str, Any]] = field(default_factory=list)
    rating_score: float = 0.0
    review_count: int = 0
    comment_count: int = 0
    main_image_local_path: str = ""
    main_image_file_name: str = ""
    main_image_mime_type: str = ""
    product_page_screenshot_local_path: str = ""
    product_page_screenshot_file_name: str = ""
    product_page_screenshot_mime_type: str = ""
    slider_captcha_resolution: dict[str, Any] = field(default_factory=dict)
    slider_captcha_audit_artifact_refs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TikTokProductRecord":
        return cls(
            source_url=str(data.get("source_url", "")),
            resolved_url=str(data.get("resolved_url", "")),
            normalized_url=str(data.get("normalized_url", "")),
            product_id=str(data.get("product_id", "")),
            title=str(data.get("title", "")),
            holiday=str(data.get("holiday", "")),
            main_image_url=str(data.get("main_image_url", "")),
            price_amount=str(data.get("price_amount", "")),
            price_currency=str(data.get("price_currency", "")),
            price_text=str(data.get("price_text", "")),
            sales_count=_coerce_int(data.get("sales_count")),
            shop_name=str(data.get("shop_name", "")),
            shop_url=str(data.get("shop_url", "")),
            gallery_images=_list_of_dicts(data.get("gallery_images")),
            sku_images=_list_of_dicts(data.get("sku_images")),
            skus=_list_of_dicts(data.get("skus")),
            sku_options=_list_of_dicts(data.get("sku_options")),
            rating_score=_coerce_float(data.get("rating_score")),
            review_count=_coerce_int(data.get("review_count")),
            comment_count=_coerce_int(data.get("comment_count")),
            main_image_local_path=str(data.get("main_image_local_path", "")),
            main_image_file_name=str(data.get("main_image_file_name", "")),
            main_image_mime_type=str(data.get("main_image_mime_type", "")),
            product_page_screenshot_local_path=str(data.get("product_page_screenshot_local_path", "")),
            product_page_screenshot_file_name=str(data.get("product_page_screenshot_file_name", "")),
            product_page_screenshot_mime_type=str(data.get("product_page_screenshot_mime_type", "")),
            slider_captcha_resolution=dict(data.get("slider_captcha_resolution") or {}),
            slider_captcha_audit_artifact_refs=_list_of_dicts(data.get("slider_captcha_audit_artifact_refs")),
        )


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _coerce_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        normalized = str(value).replace(",", "").strip().lower()
        match = re.search(r"(\d+(?:\.\d+)?)\s*([km])?", normalized)
        if not match:
            return 0
        multiplier = {"k": 1_000, "m": 1_000_000}.get(match.group(2), 1)
        return int(float(match.group(1)) * multiplier)
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        normalized = str(value).replace(",", "").strip()
        match = re.search(r"(\d+(?:\.\d+)?)", normalized)
        return float(match.group(1)) if match else 0.0
    except (TypeError, ValueError):
        return 0.0
