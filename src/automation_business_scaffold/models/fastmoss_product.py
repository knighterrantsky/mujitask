from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class FastMossProductSalesSnapshot:
    product_id: str
    search_url: str
    detail_url: str
    product_title: str
    login_state: str
    yesterday_sales: str
    sales_7d: str
    sales_28d: str
    sales_90d: str
    detail_page_screenshot_local_path: str = ""
    detail_page_screenshot_file_name: str = ""
    detail_page_screenshot_mime_type: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FastMossProductSalesSnapshot":
        return cls(
            product_id=str(data.get("product_id", "")),
            search_url=str(data.get("search_url", "")),
            detail_url=str(data.get("detail_url", "")),
            product_title=str(data.get("product_title", "")),
            login_state=str(data.get("login_state", "")),
            yesterday_sales=str(data.get("yesterday_sales", "")),
            sales_7d=str(data.get("sales_7d", "")),
            sales_28d=str(data.get("sales_28d", "")),
            sales_90d=str(data.get("sales_90d", "")),
            detail_page_screenshot_local_path=str(data.get("detail_page_screenshot_local_path", "")),
            detail_page_screenshot_file_name=str(data.get("detail_page_screenshot_file_name", "")),
            detail_page_screenshot_mime_type=str(data.get("detail_page_screenshot_mime_type", "")),
        )
