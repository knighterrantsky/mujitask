from __future__ import annotations

import mimetypes
import json
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - exercised in ad-hoc validation env.
    requests = None

from automation_business_scaffold.models import TikTokProductRecord

from .browser_bridge import open_automation_page

DEFAULT_TIKTOK_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    ),
}

DEFAULT_FEISHU_FIELD_MAPPING = {
    "source_url": "产品链接",
    "product_id": "SKU-ID",
    "main_image_file": "图片",
    "title": "标题",
    "holiday": "节日",
    "price_amount": "价格",
}
DEFAULT_IMAGE_DOWNLOAD_DIR = "runtime/downloads/tiktok_product_images"
DEFAULT_PAGE_SCREENSHOT_DIR = "runtime/downloads/tiktok_product_page_screenshots"
DEFAULT_HOLIDAY_OPTIONS = ("情人节", "复活节", "毕业季", "万圣节", "圣诞节", "其他")
HOLIDAY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "情人节": ("情人节", "valentine", "valentines", "valentine's"),
    "复活节": ("复活节", "easter"),
    "毕业季": ("毕业季", "毕业", "graduation", "graduate", "grad"),
    "万圣节": ("万圣节", "halloween"),
    "圣诞节": ("圣诞节", "christmas", "xmas"),
}
TITLE_CANDIDATE_SELECTORS = (
    "h1[data-e2e='pdp-product-title']",
    "[data-e2e='pdp-product-title']",
    "h1",
)
PRICE_CANDIDATE_SELECTORS = (
    "[data-e2e='pdp-product-price']",
    "[data-e2e='price-current']",
    "[data-e2e='product-price']",
    "[data-e2e='price-wrapper']",
)
SHOP_CANDIDATE_SELECTORS = (
    "[data-e2e='pdp-shop-name']",
    "[data-e2e='shop-name']",
    "a[href*='/shop/']",
)
MAIN_IMAGE_CANDIDATE_SELECTORS = (
    "[data-e2e='pdp-main-image'] img",
    "[data-e2e='product-image'] img",
    "div[data-e2e='pdp-main-image'] img",
    "figure img",
    "img",
)
DEFAULT_LOGIN_TOAST_SETTLE_MS = 4000


class TikTokProductExtractionError(RuntimeError):
    pass


def extract_tiktok_product_id(value: str) -> str:
    parsed = urlparse(str(value).strip())
    if "tiktok.com" not in parsed.netloc:
        return ""

    segments = [segment for segment in parsed.path.split("/") if segment]
    for marker in ("pdp", "product"):
        if marker in segments:
            index = segments.index(marker)
            for segment in segments[index + 1 :]:
                if segment.isdigit():
                    return segment

    for segment in reversed(segments):
        if segment.isdigit():
            return segment

    return ""


def normalize_tiktok_product_url(product_url: str) -> str:
    normalized_url = str(product_url).strip()
    parsed = urlparse(normalized_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("TikTok product url must start with http or https")
    if "tiktok.com" not in parsed.netloc:
        raise ValueError("TikTok product url must point to a tiktok.com domain")

    product_id = extract_tiktok_product_id(normalized_url)
    if not product_id:
        raise ValueError("TikTok product url must contain a product id")

    return f"https://www.tiktok.com/shop/pdp/{product_id}"


def fetch_tiktok_product_record(
    product_url: str,
    *,
    timeout: int = 30,
    session: Any | None = None,
) -> TikTokProductRecord:
    try:
        response = _http_get(
            product_url,
            headers=DEFAULT_TIKTOK_HEADERS,
            timeout=timeout,
            allow_redirects=True,
            session=session,
        )
    except Exception as exc:
        raise TikTokProductExtractionError(f"failed to fetch TikTok product page: {exc}") from exc

    blocked_message = _extract_blocked_message(response.text, response.headers.get("Content-Type", ""))
    if blocked_message:
        raise TikTokProductExtractionError(blocked_message)

    return extract_tiktok_product_from_html(
        response.text,
        source_url=product_url,
        resolved_url=response.url,
    )


def fetch_tiktok_product_record_via_browser(
    product_url: str,
    *,
    profile_ref: str | None = None,
    timeout_ms: int = 30000,
    capture_page_screenshot: bool = True,
) -> TikTokProductRecord:
    with open_automation_page(profile_ref=profile_ref) as browser_page:
        page = browser_page.page
        _page_goto(page, product_url)
        _wait_for_login_toast_to_settle(page)
        dom_snapshot = _wait_for_product_page_ready(page, timeout_ms=timeout_ms)
        html = _safe_page_content(page)
        resolved_url = str(getattr(page, "url", "") or product_url)
        product = _build_record_from_browser_state(
            html=html,
            dom_snapshot=dom_snapshot,
            source_url=product_url,
            resolved_url=resolved_url,
        )
        return _capture_browser_product_artifacts(
            page,
            product,
            dom_snapshot=dom_snapshot,
            capture_page_screenshot=capture_page_screenshot,
            timeout_ms=timeout_ms,
        )


def extract_tiktok_product_from_html(
    html: str,
    *,
    source_url: str,
    resolved_url: str = "",
) -> TikTokProductRecord:
    router_data = _extract_json_script(html, "__MODERN_ROUTER_DATA__")
    component_data = _find_product_component_data(router_data)

    product_info = _as_dict(component_data.get("product_info"))
    product_model = _as_dict(product_info.get("product_model"))
    promotion_model = _as_dict(product_info.get("promotion_model"))
    seller_model = _as_dict(product_info.get("seller_model"))
    shop_info = _as_dict(component_data.get("shop_info"))

    product_id = str(product_model.get("product_id", "")).strip()
    title = str(product_model.get("name", "")).strip()
    holiday = infer_tiktok_product_holiday(title)
    main_image_url = _pick_main_image_url(product_model)

    price_node = _extract_price_node(promotion_model)
    price_amount = str(
        price_node.get("sale_price_decimal")
        or price_node.get("single_product_price_decimal")
        or price_node.get("sale_price_format")
        or ""
    ).strip()
    price_currency = str(
        price_node.get("currency_name") or price_node.get("currency_symbol") or ""
    ).strip()
    price_symbol = str(price_node.get("currency_symbol", "")).strip()
    price_text = f"{price_symbol}{price_amount}" if price_symbol and price_amount else price_amount

    shop_name = str(shop_info.get("shop_name") or seller_model.get("shop_name") or "").strip()
    shop_url = str(shop_info.get("shop_link", "")).strip()
    sales_count = _parse_int(product_model.get("sold_count"))
    normalized_url = _coerce_normalized_url(source_url or resolved_url)

    if not product_id:
        raise TikTokProductExtractionError("failed to extract TikTok product id from page data")
    if not title:
        raise TikTokProductExtractionError("failed to extract TikTok product title from page data")
    if not main_image_url:
        raise TikTokProductExtractionError("failed to extract TikTok product main image from page data")
    if not price_amount:
        raise TikTokProductExtractionError("failed to extract TikTok product price from page data")
    return TikTokProductRecord(
        source_url=source_url,
        resolved_url=resolved_url or source_url,
        normalized_url=normalized_url,
        product_id=product_id,
        title=title,
        holiday=holiday,
        main_image_url=main_image_url,
        price_amount=price_amount,
        price_currency=price_currency,
        price_text=price_text,
        sales_count=sales_count,
        shop_name=shop_name,
        shop_url=shop_url,
    )


def download_tiktok_product_main_image(
    product: TikTokProductRecord,
    *,
    download_dir: str = DEFAULT_IMAGE_DOWNLOAD_DIR,
    timeout: int = 30,
    session: Any | None = None,
) -> TikTokProductRecord:
    try:
        response = _http_get(
            product.main_image_url,
            headers=DEFAULT_TIKTOK_HEADERS,
            timeout=timeout,
            session=session,
        )
        image_bytes = response.content
        if not image_bytes:
            raise TikTokProductExtractionError("downloaded TikTok product image is empty")
        content_type = str(response.headers.get("Content-Type", ""))
    except Exception as exc:
        raise TikTokProductExtractionError(f"failed to download TikTok product image: {exc}") from exc

    file_suffix = _guess_image_suffix(product.main_image_url, content_type)
    file_name = f"{product.product_id}-main-image{file_suffix}"
    target_dir = Path(download_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    local_path = target_dir / file_name
    local_path.write_bytes(image_bytes)

    mime_type = _normalize_mime_type(content_type, file_suffix)
    return replace(
        product,
        main_image_local_path=str(local_path),
        main_image_file_name=file_name,
        main_image_mime_type=mime_type,
    )


def build_feishu_bitable_fields(
    product: TikTokProductRecord,
    *,
    field_mapping: dict[str, str] | None = None,
) -> dict[str, Any]:
    logical_values: dict[str, Any] = {
        "source_url": _build_link_payload(product.source_url),
        "normalized_url": _build_link_payload(product.normalized_url),
        "product_id": product.product_id,
        "title": product.title,
        "holiday": product.holiday,
        "main_image_url": product.main_image_url,
        "main_image_local_path": product.main_image_local_path,
        "main_image_file_name": product.main_image_file_name,
        "main_image_mime_type": product.main_image_mime_type,
        "main_image_file": _build_local_file_payload(product),
        "price_amount": product.price_amount,
        "price_currency": product.price_currency,
        "price_text": product.price_text,
        "sales_count": product.sales_count,
        "shop_name": product.shop_name,
        "shop_url": product.shop_url,
        "product_page_screenshot_local_path": product.product_page_screenshot_local_path,
        "product_page_screenshot_file_name": product.product_page_screenshot_file_name,
        "product_page_screenshot_mime_type": product.product_page_screenshot_mime_type,
        "product_page_screenshot_file": _build_product_page_screenshot_payload(product),
    }

    effective_mapping = DEFAULT_FEISHU_FIELD_MAPPING | (field_mapping or {})
    fields: dict[str, Any] = {}
    for logical_key, column_name in effective_mapping.items():
        if logical_key not in logical_values:
            continue
        value = logical_values[logical_key]
        if value == "" or value == {}:
            continue
        fields[column_name] = value
    return fields


def build_feishu_bitable_record(
    product: TikTokProductRecord,
    *,
    field_mapping: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    return {
        "logical_fields": product.to_dict(),
        "fields": build_feishu_bitable_fields(product, field_mapping=field_mapping),
    }


def infer_tiktok_product_holiday(
    title: str,
    *,
    options: tuple[str, ...] = DEFAULT_HOLIDAY_OPTIONS,
) -> str:
    normalized_title = title.strip()
    if not normalized_title:
        return "其他" if "其他" in options else ""

    lowered_title = normalized_title.lower()

    for option in options:
        if option != "其他" and option in normalized_title:
            return option

    for option in options:
        if option == "其他":
            continue
        for keyword in HOLIDAY_KEYWORDS.get(option, ()):
            if keyword.lower() in lowered_title:
                return option

    return "其他" if "其他" in options else ""


def _wait_for_product_page_ready(page: Any, *, timeout_ms: int) -> dict[str, Any]:
    _wait_for_domcontentloaded(page)
    deadline = time.monotonic() + max(timeout_ms, 1000) / 1000.0
    latest_snapshot: dict[str, Any] = {}

    while time.monotonic() < deadline:
        latest_snapshot = _read_dom_product_snapshot(page)
        if int(latest_snapshot.get("visible_signal_count", 0)) >= 2:
            return latest_snapshot
        _safe_wait_for_timeout(page, 250)

    return latest_snapshot


def _build_record_from_browser_state(
    *,
    html: str,
    dom_snapshot: dict[str, Any],
    source_url: str,
    resolved_url: str,
) -> TikTokProductRecord:
    router_record: TikTokProductRecord | None = None
    if html:
        try:
            router_record = extract_tiktok_product_from_html(
                html,
                source_url=source_url,
                resolved_url=resolved_url,
            )
        except TikTokProductExtractionError:
            router_record = None

    normalized_url = _coerce_normalized_url(source_url or resolved_url)
    product_id = (
        str(dom_snapshot.get("product_id", "")).strip()
        or (router_record.product_id if router_record else "")
        or extract_tiktok_product_id(resolved_url or source_url)
    )
    title = str(dom_snapshot.get("title_text", "")).strip() or (router_record.title if router_record else "")
    main_image_url = (
        str(dom_snapshot.get("main_image_url", "")).strip()
        or (router_record.main_image_url if router_record else "")
    )
    price_text = str(dom_snapshot.get("price_text", "")).strip() or (router_record.price_text if router_record else "")
    price_amount = _normalize_price_amount(price_text) or (router_record.price_amount if router_record else "")
    price_currency = (router_record.price_currency if router_record else "") or _infer_currency_from_price_text(price_text)
    shop_name = (router_record.shop_name if router_record else "") or str(dom_snapshot.get("shop_name", "")).strip()
    shop_url = router_record.shop_url if router_record else ""
    sales_count = router_record.sales_count if router_record else 0

    if not product_id:
        raise TikTokProductExtractionError("failed to extract TikTok product id from browser page")
    if not title:
        raise TikTokProductExtractionError("failed to extract TikTok product title from browser page")
    if not main_image_url:
        raise TikTokProductExtractionError("failed to extract TikTok product main image from browser page")
    if not price_amount:
        raise TikTokProductExtractionError("failed to extract TikTok product price from browser page")

    return TikTokProductRecord(
        source_url=source_url,
        resolved_url=resolved_url or source_url,
        normalized_url=normalized_url,
        product_id=product_id,
        title=title,
        holiday=infer_tiktok_product_holiday(title),
        main_image_url=main_image_url,
        price_amount=price_amount,
        price_currency=price_currency,
        price_text=price_text or price_amount,
        sales_count=sales_count,
        shop_name=shop_name,
        shop_url=shop_url,
    )


def _capture_browser_product_artifacts(
    page: Any,
    product: TikTokProductRecord,
    *,
    dom_snapshot: dict[str, Any],
    capture_page_screenshot: bool,
    timeout_ms: int,
) -> TikTokProductRecord:
    updated = _materialize_browser_main_image(
        page,
        product,
        dom_snapshot=dom_snapshot,
        timeout_ms=timeout_ms,
    )

    if not capture_page_screenshot:
        return updated

    screenshot_dir = Path(DEFAULT_PAGE_SCREENSHOT_DIR)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    screenshot_file_name = f"{product.product_id}-product-page.png"
    screenshot_path = screenshot_dir / screenshot_file_name
    page.screenshot(path=str(screenshot_path), full_page=True)
    return replace(
        updated,
        product_page_screenshot_local_path=str(screenshot_path),
        product_page_screenshot_file_name=screenshot_file_name,
        product_page_screenshot_mime_type="image/png",
    )


def _materialize_browser_main_image(
    page: Any,
    product: TikTokProductRecord,
    *,
    dom_snapshot: dict[str, Any],
    timeout_ms: int,
) -> TikTokProductRecord:
    main_image_selector = str(dom_snapshot.get("main_image_selector", "")).strip()
    if main_image_selector:
        try:
            _wait_for_main_image_loaded(page, selector=main_image_selector, timeout_ms=timeout_ms)

            image_dir = Path(DEFAULT_IMAGE_DOWNLOAD_DIR)
            image_dir.mkdir(parents=True, exist_ok=True)
            main_image_file_name = f"{product.product_id}-main-image.png"
            main_image_path = image_dir / main_image_file_name
            _capture_locator_screenshot(page, main_image_path, selector=main_image_selector)

            return replace(
                product,
                main_image_local_path=str(main_image_path),
                main_image_file_name=main_image_file_name,
                main_image_mime_type="image/png",
            )
        except Exception:
            pass

    return download_tiktok_product_main_image(product, download_dir=DEFAULT_IMAGE_DOWNLOAD_DIR)


def _wait_for_main_image_loaded(page: Any, *, selector: str, timeout_ms: int) -> None:
    deadline = time.monotonic() + max(timeout_ms, 1000) / 1000.0
    selectors = [selector] if selector else []
    selectors.extend(MAIN_IMAGE_CANDIDATE_SELECTORS)

    while time.monotonic() < deadline:
        payload = _read_main_image_load_state(page, selectors)
        if payload.get("loaded"):
            return
        _safe_wait_for_timeout(page, 200)

    raise TikTokProductExtractionError("TikTok product main image did not finish loading before timeout")


def _capture_locator_screenshot(page: Any, target_path: Path, *, selector: str) -> None:
    candidates: list[str] = []
    if selector:
        candidates.append(selector)
    candidates.extend(MAIN_IMAGE_CANDIDATE_SELECTORS)

    last_error: Exception | None = None
    for candidate in dict.fromkeys(candidates):
        try:
            locator = page.locator(candidate)
            target = getattr(locator, "first", locator)
            wait_for = getattr(target, "wait_for", None)
            if callable(wait_for):
                wait_for(state="visible", timeout=1000)
            target.screenshot(path=str(target_path))
            return
        except Exception as exc:  # pragma: no cover - exercised via mocked pages.
            last_error = exc
            continue

    if last_error is not None:
        raise TikTokProductExtractionError(
            f"failed to capture TikTok product main image screenshot: {last_error}"
        ) from last_error
    raise TikTokProductExtractionError("failed to locate TikTok product main image element")


def _read_dom_product_snapshot(page: Any) -> dict[str, Any]:
    payload = page.evaluate(
        """(args) => {
            const selectors = {
              title: args.titleSelectors || [],
              price: args.priceSelectors || [],
              shop: args.shopSelectors || [],
              image: args.imageSelectors || [],
            };

            const isVisible = (element) => {
              if (!element) return false;
              const rect = element.getBoundingClientRect();
              const style = window.getComputedStyle(element);
              return rect.width > 0 && rect.height > 0 &&
                style.visibility !== "hidden" &&
                style.display !== "none";
            };

            const pickText = (items) => {
              for (const selector of items) {
                const element = document.querySelector(selector);
                if (!element || !isVisible(element)) continue;
                const text = (element.textContent || "").trim();
                if (text) return { text, selector };
              }
              return { text: "", selector: "" };
            };

            const pickImage = (items) => {
              for (const selector of items) {
                const element = document.querySelector(selector);
                if (!(element instanceof HTMLImageElement) || !isVisible(element)) continue;
                const src = (element.currentSrc || element.src || "").trim();
                if (!src) continue;
                return {
                  src,
                  selector,
                  loaded: Boolean(element.complete && element.naturalWidth > 0),
                };
              }
              return { src: "", selector: "", loaded: false };
            };

            const title = pickText(selectors.title);
            const price = pickText(selectors.price);
            const shop = pickText(selectors.shop);
            const image = pickImage(selectors.image);

            return {
              title_text: title.text,
              title_selector: title.selector,
              price_text: price.text,
              price_selector: price.selector,
              shop_name: shop.text,
              shop_selector: shop.selector,
              main_image_url: image.src,
              main_image_selector: image.selector,
              main_image_loaded: image.loaded,
              visible_signal_count: [Boolean(title.text), Boolean(price.text), Boolean(image.src)].filter(Boolean).length,
            };
        }""",
        {
            "titleSelectors": list(TITLE_CANDIDATE_SELECTORS),
            "priceSelectors": list(PRICE_CANDIDATE_SELECTORS),
            "shopSelectors": list(SHOP_CANDIDATE_SELECTORS),
            "imageSelectors": list(MAIN_IMAGE_CANDIDATE_SELECTORS),
        },
    )
    return payload if isinstance(payload, dict) else {}


def _read_main_image_load_state(page: Any, selectors: list[str]) -> dict[str, Any]:
    payload = page.evaluate(
        """(items) => {
            const isVisible = (element) => {
              if (!element) return false;
              const rect = element.getBoundingClientRect();
              const style = window.getComputedStyle(element);
              return rect.width > 0 && rect.height > 0 &&
                style.visibility !== "hidden" &&
                style.display !== "none";
            };

            for (const selector of items || []) {
              const element = document.querySelector(selector);
              if (!(element instanceof HTMLImageElement) || !isVisible(element)) continue;
              return {
                selector,
                loaded: Boolean(element.complete && element.naturalWidth > 0),
              };
            }

            return { selector: "", loaded: false };
        }""",
        selectors,
    )
    return payload if isinstance(payload, dict) else {}


def _extract_blocked_message(text: str, content_type: str) -> str | None:
    payload: Any | None = None
    stripped = text.lstrip()

    if "json" in content_type.lower() or stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return None

    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("msg")
        if isinstance(message, str) and message.strip():
            return message.strip()

    return None


def _extract_json_script(html: str, script_id: str) -> dict[str, Any]:
    pattern = rf'<script[^>]*id=["\']{re.escape(script_id)}["\'][^>]*>(.*?)</script>'
    match = re.search(pattern, html, flags=re.S)
    if not match:
        raise TikTokProductExtractionError(f"failed to locate script tag: {script_id}")

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise TikTokProductExtractionError(f"failed to parse script JSON: {script_id}") from exc

    if not isinstance(data, dict):
        raise TikTokProductExtractionError(f"unexpected script payload type for: {script_id}")

    return data


def _find_product_component_data(router_data: dict[str, Any]) -> dict[str, Any]:
    loader_data = _as_dict(router_data.get("loaderData"))
    for route_data in loader_data.values():
        if not isinstance(route_data, dict):
            continue
        page_config = _as_dict(route_data.get("page_config"))
        components_map = page_config.get("components_map")
        if not isinstance(components_map, list):
            continue

        for component in components_map:
            if not isinstance(component, dict):
                continue
            if component.get("component_name") != "product_info":
                continue
            component_data = component.get("component_data")
            if isinstance(component_data, dict) and component_data.get("product_info"):
                return component_data

    raise TikTokProductExtractionError("failed to locate product_info component in router data")


def _extract_price_node(promotion_model: dict[str, Any]) -> dict[str, Any]:
    promotion_product_price = _as_dict(promotion_model.get("promotion_product_price"))
    min_price = _as_dict(promotion_product_price.get("min_price"))
    if min_price:
        return min_price

    skus_price = promotion_product_price.get("skus_price")
    if isinstance(skus_price, dict):
        for price_node in skus_price.values():
            if isinstance(price_node, dict):
                return price_node

    return {}


def _pick_main_image_url(product_model: dict[str, Any]) -> str:
    images = product_model.get("images")
    if isinstance(images, list):
        for image in images:
            url = _pick_url_from_media(image)
            if url:
                return url

    sku_property_image_map = product_model.get("sku_property_image_map")
    if isinstance(sku_property_image_map, dict):
        for image in sku_property_image_map.values():
            url = _pick_url_from_media(image)
            if url:
                return url

    videos = product_model.get("videos")
    if isinstance(videos, list):
        for video in videos:
            if not isinstance(video, dict):
                continue
            url = _pick_url_from_media(video.get("cover"))
            if url:
                return url

    return ""


def _pick_url_from_media(media: Any) -> str:
    if not isinstance(media, dict):
        return ""
    url_list = media.get("url_list")
    if isinstance(url_list, list):
        for url in url_list:
            if isinstance(url, str) and url.strip():
                return url.strip()
    uri = media.get("uri")
    if isinstance(uri, str):
        return uri.strip()
    return ""


def _parse_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        digits = value.replace(",", "").strip()
        if digits.isdigit():
            return int(digits)
    return 0


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _build_local_file_payload(product: TikTokProductRecord) -> dict[str, str]:
    if not product.main_image_local_path:
        return {}
    return {
        "type": "local_file",
        "path": product.main_image_local_path,
        "file_name": product.main_image_file_name,
        "mime_type": product.main_image_mime_type,
        "source_url": product.main_image_url,
    }


def _build_product_page_screenshot_payload(product: TikTokProductRecord) -> dict[str, str]:
    if not product.product_page_screenshot_local_path:
        return {}
    return {
        "type": "local_file",
        "path": product.product_page_screenshot_local_path,
        "file_name": product.product_page_screenshot_file_name,
        "mime_type": product.product_page_screenshot_mime_type,
        "source_url": product.resolved_url,
    }


def _build_link_payload(url: str) -> dict[str, str] | str:
    normalized_url = str(url).strip()
    if not normalized_url:
        return ""
    return {
        "text": normalized_url,
        "link": normalized_url,
    }


def _guess_image_suffix(image_url: str, content_type: str) -> str:
    normalized_content_type = content_type.split(";", 1)[0].strip().lower()
    guessed_from_type = mimetypes.guess_extension(normalized_content_type, strict=False)
    if guessed_from_type:
        if guessed_from_type == ".jpe":
            return ".jpg"
        return guessed_from_type

    parsed_url = urlparse(image_url)
    parsed_suffix = Path(parsed_url.path).suffix.lower()
    if parsed_suffix:
        return parsed_suffix

    return ".jpg"


def _normalize_mime_type(content_type: str, file_suffix: str) -> str:
    normalized_content_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_content_type:
        return normalized_content_type
    guessed_type = mimetypes.guess_type(f"image{file_suffix}", strict=False)[0]
    return guessed_type or "application/octet-stream"


def _normalize_price_amount(price_value: str) -> str:
    text = str(price_value).replace(",", "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if match:
        return match.group(1)
    return ""


def _infer_currency_from_price_text(price_text: str) -> str:
    if "$" in price_text:
        return "USD"
    return ""


def _coerce_normalized_url(value: str) -> str:
    try:
        return normalize_tiktok_product_url(value)
    except ValueError:
        return ""


def _page_goto(page: Any, url: str) -> None:
    try:
        page.goto(url, wait_until="domcontentloaded")
    except TypeError:
        page.goto(url)


def _wait_for_domcontentloaded(page: Any) -> None:
    wait_for_load_state = getattr(page, "wait_for_load_state", None)
    if callable(wait_for_load_state):
        wait_for_load_state("domcontentloaded")


def _safe_wait_for_timeout(page: Any, timeout_ms: int) -> None:
    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    if callable(wait_for_timeout):
        wait_for_timeout(timeout_ms)
        return
    time.sleep(timeout_ms / 1000.0)


def _wait_for_login_toast_to_settle(page: Any, *, settle_ms: int = DEFAULT_LOGIN_TOAST_SETTLE_MS) -> None:
    _safe_wait_for_timeout(page, settle_ms)


def _safe_page_content(page: Any) -> str:
    content = getattr(page, "content", None)
    if callable(content):
        return str(content())
    return ""


class _UrllibResponse:
    def __init__(
        self,
        *,
        url: str,
        status_code: int,
        headers: dict[str, str],
        content: bytes,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.headers = headers
        self.content = content
        content_type = headers.get("Content-Type", "")
        charset_match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type)
        encoding = charset_match.group(1) if charset_match else "utf-8"
        self.text = content.decode(encoding, errors="replace")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise TikTokProductExtractionError(f"HTTP {self.status_code}")


def _http_get(
    url: str,
    *,
    headers: dict[str, str],
    timeout: int,
    allow_redirects: bool = True,
    session: Any | None = None,
) -> Any:
    if session is not None:
        response = session.get(
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )
        response.raise_for_status()
        return response

    if requests is not None:
        with requests.Session() as active_session:
            response = active_session.get(
                url,
                headers=headers,
                timeout=timeout,
                allow_redirects=allow_redirects,
            )
            response.raise_for_status()
            return response

    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            status_code = int(getattr(response, "status", 200))
            response_headers = {key: value for key, value in response.headers.items()}
            return _UrllibResponse(
                url=response.geturl(),
                status_code=status_code,
                headers=response_headers,
                content=body,
            )
    except HTTPError as exc:
        body = exc.read()
        response_headers = {key: value for key, value in exc.headers.items()}
        response = _UrllibResponse(
            url=exc.geturl(),
            status_code=exc.code,
            headers=response_headers,
            content=body,
        )
        response.raise_for_status()
        return response
    except URLError as exc:
        raise TikTokProductExtractionError(str(exc.reason)) from exc
