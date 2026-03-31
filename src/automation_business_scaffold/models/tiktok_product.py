from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class TikTokProductRecord:
    source_url: str
    resolved_url: str
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
    main_image_local_path: str = ""
    main_image_file_name: str = ""
    main_image_mime_type: str = ""

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TikTokProductRecord":
        return cls(
            source_url=str(data.get("source_url", "")),
            resolved_url=str(data.get("resolved_url", "")),
            product_id=str(data.get("product_id", "")),
            title=str(data.get("title", "")),
            holiday=str(data.get("holiday", "")),
            main_image_url=str(data.get("main_image_url", "")),
            price_amount=str(data.get("price_amount", "")),
            price_currency=str(data.get("price_currency", "")),
            price_text=str(data.get("price_text", "")),
            sales_count=int(data.get("sales_count", 0)),
            shop_name=str(data.get("shop_name", "")),
            shop_url=str(data.get("shop_url", "")),
            main_image_local_path=str(data.get("main_image_local_path", "")),
            main_image_file_name=str(data.get("main_image_file_name", "")),
            main_image_mime_type=str(data.get("main_image_mime_type", "")),
        )
