from __future__ import annotations

import json
import mimetypes
import random
import re
import time
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from automation_framework.browser import BlockedContext, BlockedHandlingConfig, BlockedResolution

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - exercised in ad-hoc validation env.
    requests = None

from automation_business_scaffold.models import TikTokProductRecord

from automation_business_scaffold.infrastructure.browser.browser_bridge import open_automation_page
from automation_business_scaffold.infrastructure.rate_limit.request_pacer import RequestPacer

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
DEFAULT_TIKTOK_SLIDER_CAPTCHA_AUDIT_DIR = "runtime/downloads/tiktok_slider_captcha_audit"
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
    "a[href*='/shop/store/']",
)
MAIN_IMAGE_CANDIDATE_SELECTORS = (
    "[data-e2e='pdp-main-image'] img",
    "[data-e2e='product-image'] img",
    "div[data-e2e='pdp-main-image'] img",
    "figure img",
)
LOGIN_TOAST_CANDIDATE_SELECTORS = (
    "[data-e2e='toast-container']",
    "[data-e2e*='toast']",
    "[data-testid*='toast']",
    "[class*='toast']",
    "[class*='Toast']",
    "[role='status']",
    "[role='alert']",
)
LOGIN_TOAST_KEYWORDS = (
    "login",
    "log in",
    "sign in",
    "signin",
    "登录",
    "登入",
    "扫码",
    "scan",
    "qr code",
)
DEFAULT_LOGIN_TOAST_SETTLE_MS = 4000
DEFAULT_LOGIN_TOAST_TIMEOUT_MS = 10000
DEFAULT_LOGIN_TOAST_POLL_MS = 250
DEFAULT_LOGIN_TOAST_STABLE_POLLS = 2
DEFAULT_SECURITY_CHECK_GRACE_MS = 10000
DEFAULT_SECURITY_CHECK_POLL_MS = 500
DEFAULT_TIKTOK_BLOCKER_PRE_DISMISS_MIN_MS = 700
DEFAULT_TIKTOK_BLOCKER_PRE_DISMISS_MAX_MS = 1600
DEFAULT_TIKTOK_BLOCKER_RETRY_MIN_MS = 180
DEFAULT_TIKTOK_BLOCKER_RETRY_MAX_MS = 420
DEFAULT_TIKTOK_BLOCKER_SETTLE_MIN_MS = 280
DEFAULT_TIKTOK_BLOCKER_SETTLE_MAX_MS = 520
DEFAULT_TIKTOK_SLIDER_CAPTCHA_MAX_ATTEMPTS = 3
DEFAULT_TIKTOK_SLIDER_CAPTCHA_APPEAR_TIMEOUT_MS = 8000
DEFAULT_TIKTOK_SLIDER_CAPTCHA_SETTLE_MS = 5000
DEFAULT_TIKTOK_SLIDER_CAPTCHA_CONFIRM_MS = 2000
DEFAULT_TIKTOK_SLIDER_CAPTCHA_REFRESH_SETTLE_MS = 2500
DEFAULT_TIKTOK_SLIDER_CAPTCHA_IMAGE_TIMEOUT_MS = 8000
DEFAULT_TIKTOK_SLIDER_CAPTCHA_DRAG_STEPS = 35
DEFAULT_TIKTOK_SLIDER_CAPTCHA_DRAG_STEP_DELAY_SECONDS = 0.03
DEFAULT_TIKTOK_SLIDER_CAPTCHA_SIMPLE_TARGET = False
DEFAULT_TIKTOK_SLIDER_CAPTCHA_POLL_MS = 250
TIKTOK_SLIDER_CAPTCHA_FAILURE_TEXTS = (
    "Unable to verify. Please try again.",
    "Unable to verify",
    "Please try again",
)
TIKTOK_LOGIN_PROMO_KEYWORDS = (
    "welcome! ready for some savings",
    "exclusive discounts",
    "create account",
    "coupon center",
)
TIKTOK_EARLY_LOGIN_PROMO_MARKERS = (
    "tiktok shop",
    "get app",
    "search",
)
SECURITY_CHECK_STRONG_SIGNALS = (
    "security check",
    "security verification",
    "verify to continue",
    "verify you are human",
    "are you human",
    "unusual traffic",
    "slide to verify",
    "drag the slider",
    "complete the verification",
    "请完成安全验证",
    "安全验证",
    "完成验证",
    "人机验证",
    "验证以继续",
    "/challenge",
)
SECURITY_CHECK_WEAK_SIGNALS = (
    "captcha",
    "secsdk-captcha",
)
SECURITY_CHECK_HTML_FALLBACK_SIGNALS = (
    "lucifer-captcha",
    "captcha/index",
    "captcha/verify",
)
TIKTOK_SLIDER_CAPTCHA_POPUP_SELECTORS = (
    "#tts_web_captcha_container",
    "#captcha_container",
    "#captcha-verify-container",
    "[id*='captcha'][style*='block']",
    "[id*='captcha']",
    "[class*='captcha'][class*='container']",
    "[class*='captcha'][class*='modal']",
    "[class*='secsdk-captcha']",
)
TIKTOK_SLIDER_CAPTCHA_BACKGROUND_SELECTORS = (
    "#captcha-verify-image",
    ".captcha_verify_img",
    ".captcha-verify-image",
    "[class*='captcha_verify_img']:not([class*='slide'])",
    "[class*='verify-image']:not([class*='slide'])",
    "[class*='captcha'] img:not([class*='slide'])",
)
TIKTOK_SLIDER_CAPTCHA_TARGET_SELECTORS = (
    ".captcha_verify_img_slide",
    ".captcha-verify-image-slide",
    "[class*='captcha_verify_img_slide']",
    "[class*='img_slide']",
    "[class*='slide'][class*='img']",
)
TIKTOK_SLIDER_CAPTCHA_HANDLE_SELECTORS = (
    ".secsdk-captcha-drag-icon",
    ".captcha_verify_slide--slidebar",
    "[class*='drag-icon']",
    "[class*='slidebar']",
    "[class*='slider'][class*='handle']",
    "[class*='captcha'] [class*='drag']",
)
TIKTOK_SLIDER_CAPTCHA_REFRESH_SELECTORS = (
    ".secsdk_captcha_refresh",
    ".captcha_verify_refresh",
    "[class*='captcha'][class*='refresh']",
    "[aria-label*='refresh' i]",
)
TIKTOK_SLIDER_CAPTCHA_SUCCESS_SELECTORS = (
    ".verify-success",
    "[class*='success'][class*='verify']",
    "[class*='captcha'][class*='success']",
)
UNAVAILABLE_PAGE_SIGNALS: tuple[tuple[str, str], ...] = (
    ("product not available in this country or region", "Product not available in this country or region"),
    ("product not available in your country or region", "Product not available in your country or region"),
    ("this product is no longer available", "This product is no longer available"),
    ("product no longer available", "Product no longer available"),
    ("this product is unavailable", "This product is unavailable"),
    ("product unavailable", "Product unavailable"),
    ("item unavailable", "Item unavailable"),
    ("product not available", "Product not available"),
    ("商品已下架", "商品已下架"),
    ("该商品已下架", "该商品已下架"),
    ("商品不存在", "商品不存在"),
    ("此商品不存在", "此商品不存在"),
    ("商品不可用", "商品不可用"),
    ("当前商品不可用", "当前商品不可用"),
    ("当前地区不可售", "当前地区不可售"),
    ("当前国家或地区不可售", "当前国家或地区不可售"),
    ("该商品在您所在地区不可售", "该商品在您所在地区不可售"),
)


class TikTokProductExtractionError(RuntimeError):
    pass


class TikTokSecurityCheckError(TikTokProductExtractionError):
    pass


class TikTokRateLimitError(TikTokProductExtractionError):
    pass


class TikTokProductUnavailableError(TikTokProductExtractionError):
    pass


def _log_tiktok_fetch_timing(*, trace_id: str, phase: str, **extra: Any) -> None:
    normalized_trace_id = str(trace_id).strip()
    if not normalized_trace_id:
        return

    epoch_ms = int(time.time() * 1000)
    detail = " ".join(
        f"{key}={str(value)}"
        for key, value in extra.items()
        if str(value or "").strip()
    )
    message = (
        f"[tiktok-fetch-timing] epoch_ms={epoch_ms} "
        f"trace_id={normalized_trace_id} phase={phase}"
    )
    if detail:
        message = f"{message} {detail}"
    print(message, flush=True)


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
    request_pacer: RequestPacer | None = None,
) -> TikTokProductRecord:
    try:
        response = _http_get(
            product_url,
            headers=DEFAULT_TIKTOK_HEADERS,
            timeout=timeout,
            allow_redirects=True,
            session=session,
            request_pacer=request_pacer,
            pacer_key="tiktok:product_page",
        )
    except TikTokProductExtractionError:
        raise
    except Exception as exc:
        raise TikTokProductExtractionError(f"failed to fetch TikTok product page: {exc}") from exc

    blocked_message = _extract_blocked_message(response.text, response.headers.get("Content-Type", ""))
    if blocked_message:
        raise TikTokProductExtractionError(blocked_message)
    unavailable_message = _extract_unavailable_message(response.text)
    if unavailable_message:
        raise TikTokProductUnavailableError(unavailable_message)

    return extract_tiktok_product_from_html(
        response.text,
        source_url=product_url,
        resolved_url=response.url,
    )


def fetch_tiktok_product_record_via_browser(
    product_url: str,
    *,
    profile_ref: str | None = None,
    workspace_id: int | None = None,
    profile_id: str | None = None,
    provider_name: str | None = None,
    timeout_ms: int = 30000,
    capture_page_screenshot: bool = True,
    security_check_grace_ms: int = DEFAULT_SECURITY_CHECK_GRACE_MS,
    slider_captcha_appear_timeout_ms: int = DEFAULT_TIKTOK_SLIDER_CAPTCHA_APPEAR_TIMEOUT_MS,
    slider_captcha_audit_dir: str = DEFAULT_TIKTOK_SLIDER_CAPTCHA_AUDIT_DIR,
    slider_captcha_provider_config: Mapping[str, Any] | None = None,
    slider_captcha_resolver_config: Mapping[str, Any] | None = None,
    slider_captcha_selectors: Mapping[str, str] | None = None,
    trace_id: str = "",
) -> TikTokProductRecord:
    _log_tiktok_fetch_timing(
        trace_id=trace_id,
        phase="browser_fetch_start",
        product_url=product_url,
    )
    with open_automation_page(
        profile_ref=profile_ref,
        workspace_id=workspace_id,
        profile_id=profile_id,
        provider_name=provider_name,
        blocked_handling=_tiktok_blocked_handling(),
    ) as browser_page:
        page = browser_page.page
        slider_resolutions: list[dict[str, Any]] = []
        _page_goto(page, product_url, timeout_ms=timeout_ms)
        _log_tiktok_fetch_timing(
            trace_id=trace_id,
            phase="page_goto_ready",
            resolved_url=str(getattr(page, "url", "") or product_url),
        )
        login_toast_timeout_ms = min(
            max(timeout_ms, DEFAULT_LOGIN_TOAST_POLL_MS),
            DEFAULT_LOGIN_TOAST_TIMEOUT_MS,
        )
        _wait_for_login_toast_to_settle(
            page,
            settle_ms=min(DEFAULT_LOGIN_TOAST_SETTLE_MS, login_toast_timeout_ms),
            timeout_ms=login_toast_timeout_ms,
        )
        _log_tiktok_fetch_timing(
            trace_id=trace_id,
            phase="login_toast_settled",
        )
        initial_html = _safe_page_content(page)
        initial_resolved_url = str(getattr(page, "url", "") or product_url)
        initial_security_check_message = _detect_browser_security_check(
            page,
            html=initial_html,
            resolved_url=initial_resolved_url,
            dom_snapshot={},
        )
        if initial_security_check_message:
            slider_resolution = _try_resolve_tiktok_slider_security_check(
                page,
                product_url=product_url,
                automation_page=browser_page,
                appear_timeout_ms=slider_captcha_appear_timeout_ms,
                audit_dir=slider_captcha_audit_dir,
                provider_config=slider_captcha_provider_config,
                resolver_config=slider_captcha_resolver_config,
                selectors=slider_captcha_selectors,
                trace_id=trace_id,
            )
            slider_resolutions.append(slider_resolution)
            _log_tiktok_fetch_timing(
                trace_id=trace_id,
                phase="security_check_slider_resolution",
                attempted=bool(slider_resolution.get("attempted")),
                resolved=bool(slider_resolution.get("resolved")),
                reason=str(slider_resolution.get("reason", "")).strip(),
                attempts=len(slider_resolution.get("attempts") or []),
                **_summarize_tiktok_slider_attempts(slider_resolution),
            )
            if not slider_resolution.get("resolved"):
                initial_html, initial_resolved_url, _initial_dom_snapshot, initial_security_check_message = (
                    _wait_for_security_check_intervention(
                        page,
                        product_url=product_url,
                        timeout_ms=security_check_grace_ms,
                    )
                )
                if initial_security_check_message:
                    raise TikTokSecurityCheckError(initial_security_check_message)
        dom_snapshot = _wait_for_product_page_ready(
            page,
            timeout_ms=timeout_ms,
            source_url=product_url,
            trace_id=trace_id,
        )
        _log_tiktok_fetch_timing(
            trace_id=trace_id,
            phase="product_page_ready",
            visible_signal_count=dom_snapshot.get("visible_signal_count", ""),
            has_title=bool(str(dom_snapshot.get("title", "")).strip()),
            has_price=bool(str(dom_snapshot.get("price_text", "")).strip()),
            has_shop=bool(str(dom_snapshot.get("shop_name", "")).strip()),
        )
        html = _safe_page_content(page)
        resolved_url = str(getattr(page, "url", "") or product_url)
        security_check_message = _detect_browser_security_check(
            page,
            html=html,
            resolved_url=resolved_url,
            dom_snapshot=dom_snapshot,
        )
        if security_check_message:
            slider_resolution = _try_resolve_tiktok_slider_security_check(
                page,
                product_url=product_url,
                automation_page=browser_page,
                appear_timeout_ms=slider_captcha_appear_timeout_ms,
                audit_dir=slider_captcha_audit_dir,
                provider_config=slider_captcha_provider_config,
                resolver_config=slider_captcha_resolver_config,
                selectors=slider_captcha_selectors,
                trace_id=trace_id,
            )
            slider_resolutions.append(slider_resolution)
            _log_tiktok_fetch_timing(
                trace_id=trace_id,
                phase="security_check_slider_resolution",
                attempted=bool(slider_resolution.get("attempted")),
                resolved=bool(slider_resolution.get("resolved")),
                reason=str(slider_resolution.get("reason", "")).strip(),
                attempts=len(slider_resolution.get("attempts") or []),
                **_summarize_tiktok_slider_attempts(slider_resolution),
            )
            if slider_resolution.get("resolved"):
                dom_snapshot = _wait_for_product_page_ready(
                    page,
                    timeout_ms=timeout_ms,
                    source_url=product_url,
                    trace_id=trace_id,
                )
                html = _safe_page_content(page)
                resolved_url = str(getattr(page, "url", "") or product_url)
                security_check_message = _detect_browser_security_check(
                    page,
                    html=html,
                    resolved_url=resolved_url,
                    dom_snapshot=dom_snapshot,
                )
        if security_check_message:
            html, resolved_url, dom_snapshot, security_check_message = _wait_for_security_check_intervention(
                page,
                product_url=product_url,
                timeout_ms=security_check_grace_ms,
            )
        if security_check_message:
            raise TikTokSecurityCheckError(security_check_message)
        _log_tiktok_fetch_timing(
            trace_id=trace_id,
            phase="security_check_cleared",
            resolved_url=resolved_url,
        )
        unavailable_message = (
            str(dom_snapshot.get("unavailable_message", "")).strip()
            or _extract_unavailable_message(html)
            or _extract_unavailable_message(_safe_body_text(page))
        )
        if unavailable_message:
            raise TikTokProductUnavailableError(unavailable_message)
        product = _build_record_from_browser_state(
            html=html,
            dom_snapshot=dom_snapshot,
            source_url=product_url,
            resolved_url=resolved_url,
        )
        product = _attach_tiktok_slider_resolution(
            product,
            slider_resolutions,
        )
        _log_tiktok_fetch_timing(
            trace_id=trace_id,
            phase="record_built",
            product_id=product.product_id,
        )
        return _capture_browser_product_artifacts(
            page,
            product,
            dom_snapshot=dom_snapshot,
            capture_page_screenshot=capture_page_screenshot,
            timeout_ms=timeout_ms,
            trace_id=trace_id,
        )


def extract_tiktok_product_from_html(
    html: str,
    *,
    source_url: str,
    resolved_url: str = "",
) -> TikTokProductRecord:
    unavailable_message = _extract_unavailable_message(html)
    if unavailable_message:
        raise TikTokProductUnavailableError(unavailable_message)
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
    gallery_images = _extract_product_gallery_images(product_model)
    sku_images = _extract_product_sku_images(product_model)
    sku_options = _extract_product_sku_options(product_info, product_model)
    skus = _extract_product_skus(
        product_info,
        product_model,
        promotion_model,
        sku_options=sku_options,
        product_id=product_id,
    )
    rating_score, review_count, comment_count = _extract_product_review_metrics(
        component_data,
        product_info,
        product_model,
    )
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
        gallery_images=gallery_images,
        sku_images=sku_images,
        skus=skus,
        sku_options=sku_options,
        rating_score=rating_score,
        review_count=review_count,
        comment_count=comment_count,
    )


def download_tiktok_product_main_image(
    product: TikTokProductRecord,
    *,
    download_dir: str = DEFAULT_IMAGE_DOWNLOAD_DIR,
    timeout: int = 30,
    session: Any | None = None,
    request_pacer: RequestPacer | None = None,
) -> TikTokProductRecord:
    try:
        response = _http_get(
            product.main_image_url,
            headers=DEFAULT_TIKTOK_HEADERS,
            timeout=timeout,
            session=session,
            request_pacer=request_pacer,
            pacer_key="tiktok:image",
        )
        image_bytes = response.content
        if not image_bytes:
            raise TikTokProductExtractionError("downloaded TikTok product image is empty")
        content_type = str(response.headers.get("Content-Type", ""))
    except TikTokProductExtractionError:
        raise
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
        "rating_score": product.rating_score,
        "review_count": product.review_count,
        "comment_count": product.comment_count,
        "gallery_images": product.gallery_images,
        "sku_images": product.sku_images,
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


def _wait_for_product_page_ready(
    page: Any,
    *,
    timeout_ms: int,
    source_url: str = "",
    trace_id: str = "",
) -> dict[str, Any]:
    _wait_for_domcontentloaded(page)
    effective_timeout_sec = max(timeout_ms, 1000) / 1000.0
    started_at = time.monotonic()
    deadline = started_at + effective_timeout_sec
    latest_snapshot: dict[str, Any] = {}
    poll_count = 0
    last_probe_signature: tuple[Any, ...] | None = None

    while time.monotonic() < deadline:
        latest_snapshot = _read_dom_product_snapshot(page)
        poll_count += 1
        resolved_url = str(getattr(page, "url", "") or source_url)
        page_html = _safe_page_content(page)
        unavailable_message = _extract_unavailable_message(page_html)
        security_check_message = _detect_browser_security_check(
            page,
            html=page_html,
            resolved_url=resolved_url,
            dom_snapshot=latest_snapshot,
        )
        capture_ready_state = _read_browser_capture_ready_state(
            html=page_html,
            dom_snapshot=latest_snapshot,
            source_url=source_url,
            resolved_url=resolved_url,
        )
        title_ready = bool(str(latest_snapshot.get("title_text", "")).strip())
        price_ready = bool(str(latest_snapshot.get("price_text", "")).strip())
        image_ready = bool(str(latest_snapshot.get("main_image_url", "")).strip())
        image_loaded = bool(latest_snapshot.get("main_image_loaded"))
        shop_ready = bool(str(latest_snapshot.get("shop_name", "")).strip())
        visible_signal_count = int(latest_snapshot.get("visible_signal_count", 0))
        waiting_for = [
            name
            for name, is_ready in (
                ("title", title_ready),
                ("price", price_ready),
                ("image", image_ready),
            )
            if not is_ready
        ]
        probe_signature = (
            visible_signal_count,
            title_ready,
            price_ready,
            image_ready,
            image_loaded,
            shop_ready,
            bool(capture_ready_state.get("ready")),
            str(capture_ready_state.get("reason", "")).strip(),
            tuple(waiting_for),
        )
        if trace_id and probe_signature != last_probe_signature:
            elapsed_ms = max(int((time.monotonic() - started_at) * 1000), 0)
            _log_tiktok_fetch_timing(
                trace_id=trace_id,
                phase="product_page_wait_probe",
                poll=poll_count,
                elapsed_ms=elapsed_ms,
                visible_signal_count=visible_signal_count,
                title_ready=title_ready,
                price_ready=price_ready,
                image_ready=image_ready,
                image_loaded=image_loaded,
                shop_ready=shop_ready,
                capture_ready=bool(capture_ready_state.get("ready")),
                capture_reason=str(capture_ready_state.get("reason", "")).strip(),
                waiting_for="|".join(waiting_for) or "ready",
            )
            last_probe_signature = probe_signature
        if title_ready and price_ready and image_ready:
            if trace_id:
                _log_tiktok_fetch_timing(
                    trace_id=trace_id,
                    phase="product_page_wait_satisfied",
                    poll=poll_count,
                    elapsed_ms=max(int((time.monotonic() - started_at) * 1000), 0),
                    visible_signal_count=visible_signal_count,
                )
            return latest_snapshot
        if unavailable_message:
            if trace_id:
                _log_tiktok_fetch_timing(
                    trace_id=trace_id,
                    phase="product_page_wait_unavailable",
                    poll=poll_count,
                    elapsed_ms=max(int((time.monotonic() - started_at) * 1000), 0),
                    unavailable_message=unavailable_message,
                )
            return {
                **latest_snapshot,
                "unavailable_message": unavailable_message,
            }
        if security_check_message:
            if trace_id:
                _log_tiktok_fetch_timing(
                    trace_id=trace_id,
                    phase="product_page_wait_security_check",
                    poll=poll_count,
                    elapsed_ms=max(int((time.monotonic() - started_at) * 1000), 0),
                    security_signal=security_check_message,
                )
            return {
                **latest_snapshot,
                "security_check_message": security_check_message,
            }
        if capture_ready_state.get("ready"):
            if trace_id:
                _log_tiktok_fetch_timing(
                    trace_id=trace_id,
                    phase="product_page_wait_capture_ready",
                    poll=poll_count,
                    elapsed_ms=max(int((time.monotonic() - started_at) * 1000), 0),
                    capture_reason=str(capture_ready_state.get("reason", "")).strip() or "record_and_image_ready",
                )
            return latest_snapshot
        _safe_wait_for_timeout(page, 250)

    if trace_id:
        _log_tiktok_fetch_timing(
            trace_id=trace_id,
            phase="product_page_wait_timeout",
            poll=poll_count,
            elapsed_ms=max(int((time.monotonic() - started_at) * 1000), 0),
            visible_signal_count=int(latest_snapshot.get("visible_signal_count", 0)),
            title_ready=bool(str(latest_snapshot.get("title_text", "")).strip()),
            price_ready=bool(str(latest_snapshot.get("price_text", "")).strip()),
            image_ready=bool(str(latest_snapshot.get("main_image_url", "")).strip()),
            image_loaded=bool(latest_snapshot.get("main_image_loaded")),
            shop_ready=bool(str(latest_snapshot.get("shop_name", "")).strip()),
        )
    return latest_snapshot


def _read_browser_capture_ready_state(
    *,
    html: str,
    dom_snapshot: dict[str, Any],
    source_url: str,
    resolved_url: str,
) -> dict[str, Any]:
    if not html.strip():
        return {
            "ready": False,
            "reason": "missing_html",
        }

    try:
        product = _build_record_from_browser_state(
            html=html,
            dom_snapshot=dom_snapshot,
            source_url=source_url,
            resolved_url=resolved_url,
        )
    except TikTokProductUnavailableError:
        raise
    except TikTokProductExtractionError as exc:
        reason = str(exc).strip().lower()
        reason = reason.removeprefix("failed to extract tiktok product ").removesuffix(" from browser page").strip()
        return {
            "ready": False,
            "reason": reason.replace(" ", "_") or "incomplete_product_data",
        }

    if not product.shop_name.strip():
        return {
            "ready": False,
            "reason": "missing_shop_name",
        }

    return {
        "ready": True,
        "reason": "record_and_image_ready",
    }


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
        except TikTokProductUnavailableError:
            raise
        except TikTokProductExtractionError:
            router_record = None

    normalized_url = _coerce_normalized_url(source_url or resolved_url)
    product_id = (
        str(dom_snapshot.get("product_id", "")).strip()
        or (router_record.product_id if router_record else "")
        or extract_tiktok_product_id(resolved_url or source_url)
    )
    title = str(dom_snapshot.get("title_text", "")).strip() or (router_record.title if router_record else "")
    main_image_url = str(dom_snapshot.get("main_image_url", "")).strip() or (
        router_record.main_image_url if router_record else ""
    )
    price_text = str(dom_snapshot.get("price_text", "")).strip() or (router_record.price_text if router_record else "")
    price_amount = _normalize_price_amount(price_text) or (router_record.price_amount if router_record else "")
    price_currency = (router_record.price_currency if router_record else "") or _infer_currency_from_price_text(price_text)
    shop_name = _clean_shop_name((router_record.shop_name if router_record else "") or str(dom_snapshot.get("shop_name", "")).strip())
    shop_url = router_record.shop_url if router_record else ""
    sales_count = (
        router_record.sales_count
        if router_record and router_record.sales_count
        else _parse_int(dom_snapshot.get("sales_count"))
    )
    rating_score = (
        router_record.rating_score
        if router_record and router_record.rating_score
        else _parse_float(dom_snapshot.get("rating_score"))
    )
    review_count = (
        router_record.review_count
        if router_record and router_record.review_count
        else _parse_int(dom_snapshot.get("review_count"))
    )
    comment_count = (
        router_record.comment_count
        if router_record and router_record.comment_count
        else _parse_int(dom_snapshot.get("comment_count")) or review_count
    )
    gallery_images = router_record.gallery_images if router_record else []
    if not gallery_images:
        gallery_images = _gallery_images_from_dom_snapshot(dom_snapshot)
    sku_images = router_record.sku_images if router_record else []
    if not sku_images:
        sku_images = _sku_images_from_dom_snapshot(dom_snapshot)
    sku_options = router_record.sku_options if router_record else []
    if not sku_options:
        sku_options = _sku_options_from_dom_snapshot(dom_snapshot)
    skus = router_record.skus if router_record else []
    if not skus:
        skus = _skus_from_dom_snapshot(dom_snapshot, product_id=product_id)

    if not product_id:
        raise TikTokProductExtractionError("failed to extract TikTok product id from browser page")
    if not title:
        raise TikTokProductExtractionError("failed to extract TikTok product title from browser page")
    if not main_image_url:
        raise TikTokProductExtractionError("failed to extract TikTok product main image from browser page")
    if not (price_amount or price_text):
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
        gallery_images=gallery_images,
        sku_images=sku_images,
        skus=skus,
        sku_options=sku_options,
        rating_score=rating_score,
        review_count=review_count,
        comment_count=comment_count,
    )


def _gallery_images_from_dom_snapshot(dom_snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_urls = dom_snapshot.get("gallery_image_urls")
    if not isinstance(raw_urls, list):
        return []
    images: list[dict[str, Any]] = []
    seen: set[str] = set()
    for display_order, value in enumerate(raw_urls):
        source_url = str(value or "").strip()
        if not source_url or source_url in seen:
            continue
        seen.add(source_url)
        images.append(
            {
                "source_url": source_url,
                "display_order": display_order + 1,
                "media_role": "product_gallery_image",
                "source_platform": "tiktok",
            }
        )
    return images


def _sku_images_from_dom_snapshot(dom_snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_images = dom_snapshot.get("sku_images")
    if not isinstance(raw_images, list):
        return []
    images: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for display_order, item in enumerate(raw_images):
        if not isinstance(item, Mapping):
            continue
        source_url = str(item.get("source_url") or item.get("url") or item.get("image_url") or "").strip()
        option_name = str(item.get("option_name") or item.get("name") or "").strip()
        option_value = str(item.get("option_value") or item.get("value") or "").strip()
        if not (source_url and option_name and option_value):
            continue
        sku_property_key = str(item.get("sku_property_key") or f"{option_name}:{option_value}").strip()
        dedupe_key = (sku_property_key.lower(), source_url)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        images.append(
            {
                "source_url": source_url,
                "display_order": _parse_int(item.get("display_order")) or display_order,
                "media_role": "product_sku_image",
                "source_platform": "tiktok",
                "sku_property_key": sku_property_key,
                "option_name": option_name,
                "option_value": option_value,
            }
        )
    return images


def _sku_options_from_dom_snapshot(dom_snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_options = dom_snapshot.get("sku_options")
    if not isinstance(raw_options, list):
        return []
    options: list[dict[str, Any]] = []
    for option in raw_options:
        if not isinstance(option, Mapping):
            continue
        option_name = str(option.get("name") or "").strip()
        raw_values = option.get("values")
        if not option_name or not isinstance(raw_values, list):
            continue
        values: list[dict[str, str]] = []
        seen_values: set[str] = set()
        for raw_value in raw_values:
            if not isinstance(raw_value, Mapping):
                continue
            option_value = str(raw_value.get("value") or "").strip()
            if not option_value or option_value.lower() in seen_values:
                continue
            seen_values.add(option_value.lower())
            values.append(
                {
                    "value": option_value,
                    "image_url": str(raw_value.get("image_url") or "").strip(),
                    "sku_property_key": str(raw_value.get("sku_property_key") or f"{option_name}:{option_value}").strip(),
                }
            )
        if values:
            options.append(
                {
                    "name": option_name,
                    "values": values,
                    "source_platform": "tiktok",
                }
            )
    return options


def _skus_from_dom_snapshot(dom_snapshot: Mapping[str, Any], *, product_id: str) -> list[dict[str, Any]]:
    sku_options = _sku_options_from_dom_snapshot(dom_snapshot)
    if len(sku_options) != 1:
        return []
    option_name = str(sku_options[0].get("name") or "").strip()
    skus: list[dict[str, Any]] = []
    for value in sku_options[0].get("values") or []:
        if not isinstance(value, Mapping):
            continue
        option_value = str(value.get("value") or "").strip()
        if not option_value:
            continue
        sku_property_key = str(value.get("sku_property_key") or f"{option_name}:{option_value}").strip()
        skus.append(
            {
                "product_id": product_id,
                "sku_id": "",
                "sku_name": option_value,
                "spec_name": f"{option_name}: {option_value}",
                "properties": [
                    {
                        "name": option_name,
                        "value": option_value,
                        "sku_property_key": sku_property_key,
                        "image_url": str(value.get("image_url") or "").strip(),
                    }
                ],
                "sku_property_keys": [sku_property_key],
                "source_platform": "tiktok",
            }
        )
    return skus


def _capture_browser_product_artifacts(
    page: Any,
    product: TikTokProductRecord,
    *,
    dom_snapshot: dict[str, Any],
    capture_page_screenshot: bool,
    timeout_ms: int,
    trace_id: str = "",
) -> TikTokProductRecord:
    updated = _materialize_browser_main_image(
        page,
        product,
        dom_snapshot=dom_snapshot,
        timeout_ms=timeout_ms,
        trace_id=trace_id,
    )
    _log_tiktok_fetch_timing(
        trace_id=trace_id,
        phase="main_image_ready",
        product_id=updated.product_id,
        main_image_file_name=updated.main_image_file_name,
    )

    if not capture_page_screenshot:
        _log_tiktok_fetch_timing(
            trace_id=trace_id,
            phase="browser_fetch_complete",
            product_id=updated.product_id,
            capture_page_screenshot=False,
        )
        return updated

    screenshot_dir = Path(DEFAULT_PAGE_SCREENSHOT_DIR)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    screenshot_file_name = f"{product.product_id}-product-page.png"
    screenshot_path = screenshot_dir / screenshot_file_name
    page.screenshot(path=str(screenshot_path), full_page=True)
    final_product = replace(
        updated,
        product_page_screenshot_local_path=str(screenshot_path),
        product_page_screenshot_file_name=screenshot_file_name,
        product_page_screenshot_mime_type="image/png",
    )
    _log_tiktok_fetch_timing(
        trace_id=trace_id,
        phase="page_screenshot_ready",
        product_id=final_product.product_id,
        screenshot_file_name=screenshot_file_name,
    )
    _log_tiktok_fetch_timing(
        trace_id=trace_id,
        phase="browser_fetch_complete",
        product_id=final_product.product_id,
        capture_page_screenshot=True,
    )
    return final_product


def _materialize_browser_main_image(
    page: Any,
    product: TikTokProductRecord,
    *,
    dom_snapshot: dict[str, Any],
    timeout_ms: int,
    trace_id: str = "",
) -> TikTokProductRecord:
    main_image_selector = str(dom_snapshot.get("main_image_selector", "")).strip()
    try:
        downloaded = download_tiktok_product_main_image(
            product,
            download_dir=DEFAULT_IMAGE_DOWNLOAD_DIR,
        )
        _log_tiktok_fetch_timing(
            trace_id=trace_id,
            phase="main_image_download_ready",
            product_id=downloaded.product_id,
        )
        return downloaded
    except Exception:
        pass

    screenshot_selector = main_image_selector if main_image_selector and main_image_selector != "img" else ""
    if not screenshot_selector and product.main_image_url:
        screenshot_selector = _mark_matching_main_image_element(
            page,
            expected_url=product.main_image_url,
            selectors=[main_image_selector, *MAIN_IMAGE_CANDIDATE_SELECTORS],
        )

    if screenshot_selector:
        try:
            _wait_for_main_image_loaded(page, selector=screenshot_selector, timeout_ms=timeout_ms)

            image_dir = Path(DEFAULT_IMAGE_DOWNLOAD_DIR)
            image_dir.mkdir(parents=True, exist_ok=True)
            main_image_file_name = f"{product.product_id}-main-image.png"
            main_image_path = image_dir / main_image_file_name
            _capture_locator_screenshot(page, main_image_path, selector=screenshot_selector)

            captured = replace(
                product,
                main_image_local_path=str(main_image_path),
                main_image_file_name=main_image_file_name,
                main_image_mime_type="image/png",
            )
            _log_tiktok_fetch_timing(
                trace_id=trace_id,
                phase="main_image_screenshot_ready",
                product_id=captured.product_id,
                selector=screenshot_selector,
            )
            return captured
        except Exception:
            pass

    raise TikTokProductExtractionError("failed to materialize TikTok product main image")


def _mark_matching_main_image_element(
    page: Any,
    *,
    expected_url: str,
    selectors: list[str],
) -> str:
    payload = page.evaluate(
        """(args) => {
            const expectedUrl = String(args.expectedUrl || "").trim();
            const candidateSelectors = Array.isArray(args.selectors) ? args.selectors : [];
            const markerAttr = "data-mujitask-main-image-target";

            const isVisible = (element) => {
              if (!element) return false;
              const rect = element.getBoundingClientRect();
              const style = window.getComputedStyle(element);
              return rect.width > 0 && rect.height > 0 &&
                style.visibility !== "hidden" &&
                style.display !== "none";
            };

            const normalizeUrl = (value) => {
              const raw = String(value || "").trim();
              if (!raw) return "";
              try {
                const url = new URL(raw, window.location.href);
                url.hash = "";
                url.search = "";
                return url.toString();
              } catch (_error) {
                return raw.split("#")[0].split("?")[0];
              }
            };

            const collectImages = () => {
              const seen = new Set();
              const images = [];
              for (const selector of candidateSelectors) {
                if (!selector) continue;
                const nodes = document.querySelectorAll(selector);
                for (const node of nodes) {
                  if (!(node instanceof HTMLImageElement) || !isVisible(node)) continue;
                  if (seen.has(node)) continue;
                  seen.add(node);
                  images.push(node);
                }
              }
              return images;
            };

            for (const node of document.querySelectorAll(`[${markerAttr}]`)) {
              node.removeAttribute(markerAttr);
            }

            const normalizedExpected = normalizeUrl(expectedUrl);
            if (!normalizedExpected) {
              return { selector: "" };
            }

            const expectedPath = (() => {
              try {
                return new URL(normalizedExpected).pathname || "";
              } catch (_error) {
                return "";
              }
            })();

            for (const image of collectImages()) {
              const current = normalizeUrl(image.currentSrc || image.src || "");
              if (!current) continue;
              if (current === normalizedExpected) {
                image.setAttribute(markerAttr, "1");
                return { selector: `img[${markerAttr}="1"]` };
              }
              if (expectedPath) {
                try {
                  const currentPath = new URL(current).pathname || "";
                  if (currentPath && currentPath === expectedPath) {
                    image.setAttribute(markerAttr, "1");
                    return { selector: `img[${markerAttr}="1"]` };
                  }
                } catch (_error) {
                  // Ignore malformed current URLs and continue.
                }
              }
            }

            return { selector: "" };
        }""",
        {
            "expectedUrl": expected_url,
            "selectors": [item for item in selectors if item],
        },
    )
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("selector", "")).strip()


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


def _tiktok_blocked_handling() -> BlockedHandlingConfig:
    return BlockedHandlingConfig(handler=_handle_tiktok_blocked_context)


def _handle_tiktok_blocked_context(automation_page: Any, event: BlockedContext) -> BlockedResolution:
    page = getattr(automation_page, "raw_page", None) or getattr(automation_page, "page", None) or automation_page
    if _is_tiktok_slider_security_blocker(event):
        slider_resolution = _try_resolve_tiktok_slider_security_check(
            page,
            product_url=str(getattr(event, "page_url", "") or getattr(page, "url", "") or ""),
        )
        if slider_resolution.get("resolved"):
            return BlockedResolution.handled_recheck("resolved TikTok product slider security verification")

    if not _is_tiktok_login_promo_blocker(event):
        return BlockedResolution.resume_default()

    if _dismiss_tiktok_login_promo(page):
        if _tiktok_product_content_is_visible(page):
            return BlockedResolution.force_continue(
                "dismissed TikTok login promo popover and product content is visible"
            )
        if str(getattr(event, "detection_source", "") or "").strip().lower() == "body":
            return BlockedResolution.force_continue(
                "dismissed TikTok login promo popover from body-level blocker probe"
            )
        return BlockedResolution.handled_recheck("dismissed TikTok login promo popover")
    if _tiktok_product_content_is_visible(page):
        return BlockedResolution.force_continue("ignored non-blocking TikTok login promo popover")
    return BlockedResolution.resume_default()


def _is_tiktok_slider_security_blocker(event: BlockedContext) -> bool:
    page_url = str(getattr(event, "page_url", "") or "").lower()
    if "tiktok.com/shop/" not in page_url:
        return False

    blocker_type = str(getattr(event, "blocker_type", "") or "").strip().lower()
    if blocker_type == "security_challenge":
        return True

    candidate_texts = _collect_tiktok_blocked_text_candidates(event)
    security_signals = SECURITY_CHECK_STRONG_SIGNALS + SECURITY_CHECK_WEAK_SIGNALS + (
        "secsdk",
        "slider",
    )
    return any(signal in text for text in candidate_texts for signal in security_signals)


def _try_resolve_tiktok_slider_security_check(
    page: Any,
    *,
    product_url: str,
    automation_page: Any | None = None,
    max_attempts: int = DEFAULT_TIKTOK_SLIDER_CAPTCHA_MAX_ATTEMPTS,
    appear_timeout_ms: int = DEFAULT_TIKTOK_SLIDER_CAPTCHA_APPEAR_TIMEOUT_MS,
    settle_ms: int = DEFAULT_TIKTOK_SLIDER_CAPTCHA_SETTLE_MS,
    confirm_ms: int = DEFAULT_TIKTOK_SLIDER_CAPTCHA_CONFIRM_MS,
    audit_dir: str = DEFAULT_TIKTOK_SLIDER_CAPTCHA_AUDIT_DIR,
    provider_config: Mapping[str, Any] | None = None,
    resolver_config: Mapping[str, Any] | None = None,
    selectors: Mapping[str, str] | None = None,
    trace_id: str = "",
) -> dict[str, Any]:
    effective_max_attempts = max(int(max_attempts), 0)
    if effective_max_attempts <= 0:
        return {"attempted": False, "resolved": False, "reason": "disabled", "attempts": []}

    state = _wait_for_tiktok_slider_captcha_state(page, timeout_ms=appear_timeout_ms)
    if not state.get("visible"):
        return {"attempted": False, "resolved": False, "reason": "slider_not_visible", "attempts": []}

    if automation_page is not None:
        return _resolve_tiktok_slider_with_framework_captcha(
            automation_page,
            page=page,
            product_url=product_url,
            max_attempts=effective_max_attempts,
            settle_ms=settle_ms,
            confirm_ms=confirm_ms,
            audit_dir=audit_dir,
            provider_config=provider_config,
            resolver_config=resolver_config,
            selectors=selectors,
            trace_id=trace_id,
        )

    try:
        captcha_provider = _build_tiktok_slider_captcha_provider()
    except Exception as exc:  # noqa: BLE001
        return {
            "attempted": True,
            "resolved": False,
            "reason": "captcha_provider_unavailable",
            "error": str(exc),
            "attempts": [],
        }

    attempts: list[dict[str, Any]] = []
    for attempt_index in range(1, effective_max_attempts + 1):
        attempt: dict[str, Any] = {"attempt": attempt_index}
        attempts.append(attempt)
        try:
            if attempt_index > 1:
                _click_first_visible_locator(page, TIKTOK_SLIDER_CAPTCHA_REFRESH_SELECTORS)
                _safe_wait_for_timeout(page, DEFAULT_TIKTOK_SLIDER_CAPTCHA_REFRESH_SETTLE_MS)

            state = _read_tiktok_slider_captcha_state(page)
            if not state.get("visible"):
                attempt["resolved_before_drag"] = True
                _safe_wait_for_timeout(page, max(int(confirm_ms), 1))
                confirmed_state = _read_tiktok_slider_captcha_state(page)
                attempt["confirmation_wait_ms"] = max(int(confirm_ms), 1)
                attempt["confirmation_popup_still_visible"] = bool(confirmed_state.get("visible"))
                if confirmed_state.get("visible"):
                    attempt["reason"] = "slider_reappeared_after_confirmation_wait"
                    continue
                return {
                    "attempted": True,
                    "resolved": True,
                    "reason": "slider_already_cleared",
                    "attempts": attempts,
                }

            background_locator, background_selector = _first_visible_locator(
                page,
                TIKTOK_SLIDER_CAPTCHA_BACKGROUND_SELECTORS,
            )
            target_locator, target_selector = _first_visible_locator(
                page,
                TIKTOK_SLIDER_CAPTCHA_TARGET_SELECTORS,
            )
            handle_locator, handle_selector = _first_visible_locator(
                page,
                TIKTOK_SLIDER_CAPTCHA_HANDLE_SELECTORS,
            )
            if not (background_locator and target_locator and handle_locator):
                attempt["reason"] = "missing_slider_elements"
                continue

            background_box = _locator_bounding_box(background_locator)
            target_box = _locator_bounding_box(target_locator)
            handle_box = _locator_bounding_box(handle_locator)
            target_hidden_for_background = _hide_locator_for_visual_capture(target_locator)
            try:
                background_image = _locator_screenshot_bytes(background_locator)
            finally:
                if target_hidden_for_background:
                    _restore_locator_after_visual_capture(target_locator)
            target_image = _locator_screenshot_bytes(target_locator)
            if not (background_image and target_image and background_box and handle_box):
                attempt["reason"] = "missing_slider_artifacts"
                continue

            slider_match, match_metadata = _match_tiktok_slider(
                captcha_provider,
                target_image,
                background_image,
            )
            drag_distance = _calculate_tiktok_slider_drag_distance(
                slider_match=slider_match,
                background_image=background_image,
                background_box=background_box,
                target_box=target_box,
                handle_box=handle_box,
            )
            attempt.update(
                {
                    "background_selector": background_selector,
                    "target_selector": target_selector,
                    "handle_selector": handle_selector,
                    "target_x": int(getattr(slider_match, "target_x", 0)),
                    "target_y": int(getattr(slider_match, "target_y", 0)),
                    "confidence": getattr(slider_match, "confidence", None),
                    **match_metadata,
                    "drag_distance": round(drag_distance, 2),
                }
            )
            _drag_tiktok_slider_handle(page, handle_box=handle_box, drag_distance=drag_distance)
            state = _wait_for_tiktok_slider_post_drag_state(page, timeout_ms=max(int(settle_ms), 1))
            attempt["post_drag_verify_wait_ms"] = max(int(settle_ms), 1)
            attempt["post_drag_wait_elapsed_ms"] = state.get("wait_elapsed_ms")
            attempt["popup_still_visible"] = bool(state.get("visible"))
            if state.get("failure_text"):
                attempt["failure_text"] = state.get("failure_text")
                attempt["reason"] = "slider_verification_failed_text"
                continue
            if not state.get("visible") or state.get("success"):
                confirmed_state = _confirm_tiktok_slider_cleared(page, confirm_ms=max(int(confirm_ms), 1))
                attempt.update(confirmed_state)
                if confirmed_state.get("confirmation_popup_still_visible"):
                    attempt["reason"] = "slider_reappeared_after_confirmation_wait"
                    continue
                if confirmed_state.get("confirmation_failure_text"):
                    attempt["reason"] = "slider_verification_failed_text"
                    continue
                _log_tiktok_fetch_timing(
                    trace_id=trace_id,
                    phase="slider_security_check_resolved",
                    product_url=product_url,
                    attempt=attempt_index,
                    drag_distance=round(drag_distance, 2),
                )
                return {
                    "attempted": True,
                    "resolved": True,
                    "reason": "slider_cleared",
                    "attempts": attempts,
                }
        except Exception as exc:  # noqa: BLE001
            attempt["reason"] = "slider_attempt_failed"
            attempt["error"] = str(exc)

    return {
        "attempted": True,
        "resolved": False,
        "reason": "slider_popup_still_visible",
        "attempts": attempts,
    }


def _summarize_tiktok_slider_attempts(slider_resolution: Mapping[str, Any]) -> dict[str, Any]:
    attempts = slider_resolution.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return {}
    last_attempt = attempts[-1] if isinstance(attempts[-1], Mapping) else {}
    reason_counts: dict[str, int] = {}
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        reason = str(attempt.get("reason") or "").strip() or "unknown"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "attempt_reasons": ",".join(f"{reason}:{count}" for reason, count in sorted(reason_counts.items())),
        "last_background_selector": str(last_attempt.get("background_selector") or ""),
        "last_target_selector": str(last_attempt.get("target_selector") or ""),
        "last_handle_selector": str(last_attempt.get("handle_selector") or ""),
        "last_target_x": last_attempt.get("target_x", ""),
        "last_target_y": last_attempt.get("target_y", ""),
        "last_confidence": last_attempt.get("confidence", ""),
        "last_match_method": last_attempt.get("match_method", ""),
        "last_simple_target": last_attempt.get("simple_target", ""),
        "last_drag_distance": last_attempt.get("drag_distance", ""),
        "last_popup_still_visible": last_attempt.get("popup_still_visible", ""),
        "last_confirmation_popup_still_visible": last_attempt.get("confirmation_popup_still_visible", ""),
    }


def _resolve_tiktok_slider_with_framework_captcha(
    automation_page: Any,
    *,
    page: Any,
    product_url: str,
    max_attempts: int,
    settle_ms: int,
    confirm_ms: int,
    audit_dir: str,
    provider_config: Mapping[str, Any] | None,
    resolver_config: Mapping[str, Any] | None,
    selectors: Mapping[str, str] | None,
    trace_id: str,
) -> dict[str, Any]:
    from automation_framework.captcha import (
        DdddOcrCaptchaProvider,
        SliderCaptchaResolveConfig,
        SliderCaptchaResolver,
        SliderCaptchaSelectors,
    )

    selector_payload = {
        "popup": "#tts_web_captcha_container",
        "background": "#captcha-verify-image",
        "piece": ".captcha_verify_img_slide",
        "handle": ".secsdk-captcha-drag-icon",
        "refresh": ".secsdk_captcha_refresh",
        **dict(selectors or {}),
    }
    resolver_overrides = dict(resolver_config or {})
    post_drag_poll_ms = max(int(resolver_overrides.pop("after_drag_wait_ms", settle_ms)), 1)
    refresh_wait_ms = max(int(resolver_overrides.pop("refresh_wait_ms", DEFAULT_TIKTOK_SLIDER_CAPTCHA_REFRESH_SETTLE_MS)), 0)
    image_timeout_ms = max(int(resolver_overrides.pop("image_timeout_ms", DEFAULT_TIKTOK_SLIDER_CAPTCHA_IMAGE_TIMEOUT_MS)), 1)
    resolver_overrides.pop("success_timeout_ms", None)
    resolver_overrides.pop("max_attempts", None)
    config_payload = {
        "max_attempts": 1,
        "image_timeout_ms": image_timeout_ms,
        "refresh_wait_ms": refresh_wait_ms,
        "after_drag_wait_ms": 0,
        "success_timeout_ms": 0,
        "drag_steps": DEFAULT_TIKTOK_SLIDER_CAPTCHA_DRAG_STEPS,
        "drag_step_delay_seconds": DEFAULT_TIKTOK_SLIDER_CAPTCHA_DRAG_STEP_DELAY_SECONDS,
        "simple_target": DEFAULT_TIKTOK_SLIDER_CAPTCHA_SIMPLE_TARGET,
        "capture_page_screenshots": True,
        "capture_image_artifacts": True,
        **resolver_overrides,
    }
    provider = DdddOcrCaptchaProvider(**dict(provider_config or {}))
    selector_model = SliderCaptchaSelectors(**selector_payload)
    artifact_refs: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    raw_attempts: list[dict[str, Any]] = []
    audit_payload: dict[str, Any] = {
        "config": dict(config_payload),
        "selectors": selector_model.model_dump(mode="json"),
        "success": False,
        "attempts": raw_attempts,
    }
    reason = "slider_popup_still_visible"
    resolved = False
    confirmation_wait_ms = max(int(confirm_ms), 1)
    for attempt_index in range(1, max(int(max_attempts), 1) + 1):
        if attempt_index > 1:
            _click_first_visible_locator(page, _selector_candidates(str(selector_payload.get("refresh") or ""), TIKTOK_SLIDER_CAPTCHA_REFRESH_SELECTORS))
            if refresh_wait_ms:
                _safe_wait_for_timeout(page, refresh_wait_ms)
        resolver = SliderCaptchaResolver(
            provider=provider,
            selectors=selector_model,
            config=SliderCaptchaResolveConfig(**config_payload),
        )
        resolution = resolver.resolve(automation_page)
        current_audit = resolution.audit.model_dump(mode="json")
        current_artifact_refs = _persist_tiktok_slider_artifacts_payload(
            resolution.artifacts_payload,
            audit_dir=audit_dir,
            product_url=product_url,
            trace_id=f"{trace_id or ''}-attempt-{attempt_index}".strip("-"),
        )
        artifact_refs.extend(current_artifact_refs)
        current_raw_attempts = current_audit.get("attempts")
        for raw_attempt in current_raw_attempts if isinstance(current_raw_attempts, list) else []:
            if isinstance(raw_attempt, Mapping):
                item = dict(raw_attempt)
                item["attempt_index"] = attempt_index
                raw_attempts.append(item)
        current_records = _framework_slider_attempts_from_audit(
            {"attempts": [raw_attempts[-1]]} if raw_attempts else current_audit,
            post_drag_verify_wait_ms=post_drag_poll_ms,
            confirmation_wait_ms=0,
            confirmation_popup_still_visible=None,
        )
        record = current_records[-1] if current_records else {"attempt": attempt_index}
        record["attempt"] = attempt_index
        state = _wait_for_tiktok_slider_post_drag_state(page, timeout_ms=post_drag_poll_ms)
        record["post_drag_verify_wait_ms"] = post_drag_poll_ms
        record["post_drag_wait_elapsed_ms"] = state.get("wait_elapsed_ms")
        record["popup_still_visible"] = bool(state.get("visible"))
        if state.get("failure_text"):
            record["failure_text"] = state.get("failure_text")
            record["reason"] = "slider_verification_failed_text"
            attempts.append(record)
            reason = "slider_verification_failed_text"
            continue
        if not state.get("visible") or state.get("success"):
            confirmed_state = _confirm_tiktok_slider_cleared(page, confirm_ms=confirmation_wait_ms)
            record.update(confirmed_state)
            if confirmed_state.get("confirmation_popup_still_visible"):
                record["reason"] = "slider_reappeared_after_confirmation_wait"
                attempts.append(record)
                reason = "slider_reappeared_after_confirmation_wait"
                continue
            if confirmed_state.get("confirmation_failure_text"):
                record["reason"] = "slider_verification_failed_text"
                attempts.append(record)
                reason = "slider_verification_failed_text"
                continue
            record["reason"] = "slider_cleared"
            attempts.append(record)
            resolved = True
            reason = "slider_cleared"
            break
        record["reason"] = "slider_popup_still_visible"
        attempts.append(record)
        reason = "slider_popup_still_visible"
    audit_payload["success"] = resolved
    return {
        "attempted": True,
        "resolved": resolved,
        "reason": reason,
        "attempts": attempts,
        "framework_resolver": "SliderCaptchaResolver",
        "post_drag_verify_wait_ms": post_drag_poll_ms,
        "confirmation_wait_ms": confirmation_wait_ms,
        "drag_profile": {
            "steps": int(config_payload["drag_steps"]),
            "step_delay_seconds": float(config_payload["drag_step_delay_seconds"]),
        },
        "audit": audit_payload,
        "artifact_refs": artifact_refs,
    }


def _framework_slider_attempts_from_audit(
    audit_payload: Mapping[str, Any],
    *,
    post_drag_verify_wait_ms: int,
    confirmation_wait_ms: int,
    confirmation_popup_still_visible: bool | None,
) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    raw_attempts = audit_payload.get("attempts")
    for item in raw_attempts if isinstance(raw_attempts, list) else []:
        attempt = item if isinstance(item, Mapping) else {}
        mapping = attempt.get("mapping") if isinstance(attempt.get("mapping"), Mapping) else {}
        slider_result = attempt.get("slider_result") if isinstance(attempt.get("slider_result"), Mapping) else {}
        background = attempt.get("background") if isinstance(attempt.get("background"), Mapping) else {}
        piece = attempt.get("piece") if isinstance(attempt.get("piece"), Mapping) else {}
        record = {
            "attempt": attempt.get("attempt_index"),
            "reason": "" if attempt.get("success") else str(attempt.get("error") or "slider_attempt_failed"),
            "match_method": "framework_slider_resolver",
            "mode": attempt.get("mode"),
            "simple_target": attempt.get("simple_target"),
            "target_x": slider_result.get("target_x"),
            "target_y": slider_result.get("target_y"),
            "confidence": slider_result.get("confidence"),
            "raw_result": slider_result.get("raw"),
            "coordinate_mapping": mapping,
            "drag_distance": mapping.get("drag_distance"),
            "popup_still_visible": attempt.get("popup_still_visible"),
            "selector_success": attempt.get("selector_success"),
            "post_drag_verify_wait_ms": post_drag_verify_wait_ms,
            "artifact_keys": {
                "background": background.get("artifact_key"),
                "piece": piece.get("artifact_key"),
                "before_screenshot": attempt.get("before_screenshot_key"),
                "after_screenshot": attempt.get("after_screenshot_key"),
            },
        }
        if attempt.get("success") and confirmation_wait_ms:
            record["confirmation_wait_ms"] = confirmation_wait_ms
            record["confirmation_popup_still_visible"] = bool(confirmation_popup_still_visible)
            if confirmation_popup_still_visible:
                record["reason"] = "slider_reappeared_after_confirmation_wait"
        attempts.append(record)
    return attempts


def _selector_candidates(primary: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    normalized = str(primary or "").strip()
    if not normalized:
        return fallback
    return (normalized, *tuple(selector for selector in fallback if selector != normalized))


def _wait_for_tiktok_slider_post_drag_state(
    page: Any,
    *,
    timeout_ms: int,
    poll_ms: int = DEFAULT_TIKTOK_SLIDER_CAPTCHA_POLL_MS,
) -> dict[str, Any]:
    effective_timeout_ms = max(int(timeout_ms), 0)
    effective_poll_ms = max(int(poll_ms), 1)
    elapsed_ms = 0
    last_state: dict[str, Any] = {}
    while True:
        state = _read_tiktok_slider_captcha_state(page)
        failure_text = _read_tiktok_slider_failure_text(page)
        last_state = {
            **state,
            "failure_text": failure_text,
            "wait_elapsed_ms": elapsed_ms,
        }
        if failure_text or not state.get("visible") or state.get("success") or elapsed_ms >= effective_timeout_ms:
            return last_state
        wait_ms = min(effective_poll_ms, effective_timeout_ms - elapsed_ms)
        if wait_ms <= 0:
            return last_state
        _safe_wait_for_timeout(page, wait_ms)
        elapsed_ms += wait_ms


def _confirm_tiktok_slider_cleared(page: Any, *, confirm_ms: int) -> dict[str, Any]:
    wait_ms = max(int(confirm_ms), 1)
    _safe_wait_for_timeout(page, wait_ms)
    confirmed_state = _read_tiktok_slider_captcha_state(page)
    failure_text = _read_tiktok_slider_failure_text(page)
    return {
        "confirmation_wait_ms": wait_ms,
        "confirmation_popup_still_visible": bool(confirmed_state.get("visible")),
        "confirmation_failure_text": failure_text,
    }


def _read_tiktok_slider_failure_text(page: Any) -> str:
    for text in TIKTOK_SLIDER_CAPTCHA_FAILURE_TEXTS:
        selector = f"text={text}"
        try:
            locator = page.locator(selector)
            target = getattr(locator, "first", locator)
            if _locator_is_visible(target, timeout_ms=250):
                return text
        except Exception:
            continue
    return ""


def _persist_tiktok_slider_artifacts_payload(
    artifacts_payload: Mapping[str, Any],
    *,
    audit_dir: str,
    product_url: str,
    trace_id: str,
) -> list[dict[str, Any]]:
    root = Path(audit_dir or DEFAULT_TIKTOK_SLIDER_CAPTCHA_AUDIT_DIR)
    product_key = extract_tiktok_product_id(product_url) or "unknown-product"
    run_key = re.sub(r"[^a-zA-Z0-9_.-]+", "-", trace_id or str(int(time.time() * 1000))).strip("-")
    target_dir = root / product_key / run_key
    target_dir.mkdir(parents=True, exist_ok=True)
    refs: list[dict[str, Any]] = []

    state_dump = artifacts_payload.get("state_dump")
    if state_dump:
        refs.append(_write_tiktok_slider_audit_file(target_dir / "slider_captcha_audit.json", state_dump))

    extra = artifacts_payload.get("extra") if isinstance(artifacts_payload.get("extra"), Mapping) else {}
    for key, value in extra.items():
        safe_key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(key)).strip("_") or "artifact"
        if isinstance(value, bytes):
            refs.append(_write_tiktok_slider_binary_file(target_dir / f"{safe_key}.bin", value, artifact_key=str(key)))
        elif key == "slider_captcha_audit":
            continue
        elif isinstance(value, (dict, list, str, int, float, bool)) or value is None:
            refs.append(_write_tiktok_slider_audit_file(target_dir / f"{safe_key}.json", value, artifact_key=str(key)))
    return refs


def _write_tiktok_slider_audit_file(path: Path, value: Any, *, artifact_key: str = "slider_captcha_audit") -> dict[str, Any]:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "artifact_key": artifact_key,
        "local_path": str(path),
        "file_name": path.name,
        "mime_type": "application/json",
    }


def _write_tiktok_slider_binary_file(path: Path, value: bytes, *, artifact_key: str) -> dict[str, Any]:
    suffix = ".png" if value.startswith(b"\x89PNG") else ".jpg" if value.startswith(b"\xff\xd8") else ".bin"
    final_path = path.with_suffix(suffix)
    final_path.write_bytes(value)
    return {
        "artifact_key": artifact_key,
        "local_path": str(final_path),
        "file_name": final_path.name,
        "mime_type": "image/png" if suffix == ".png" else "image/jpeg" if suffix == ".jpg" else "application/octet-stream",
    }


def _attach_tiktok_slider_resolution(
    product: TikTokProductRecord,
    slider_resolutions: list[dict[str, Any]],
) -> TikTokProductRecord:
    attempted = [item for item in slider_resolutions if item.get("attempted")]
    if not attempted:
        return product
    latest = attempted[-1]
    return replace(
        product,
        slider_captcha_resolution=latest,
        slider_captcha_audit_artifact_refs=[
            dict(item) for item in latest.get("artifact_refs", []) if isinstance(item, Mapping)
        ],
    )


def _build_tiktok_slider_captcha_provider() -> Any:
    from automation_framework.captcha import DdddOcrCaptchaProvider

    return DdddOcrCaptchaProvider()


def _match_tiktok_slider(
    captcha_provider: Any,
    target_image: bytes,
    background_image: bytes,
) -> tuple[Any, dict[str, Any]]:
    gap = _detect_tiktok_slider_gap_from_background(background_image)
    if gap:
        return (
            SimpleNamespace(
                target_x=int(gap["x"]),
                target_y=int(gap["y"]),
                confidence=1.0,
                raw={"method": "dark_gap_component", **gap},
            ),
            {
                "match_method": "dark_gap_component",
                "simple_target": "",
                "gap_width": gap.get("width", ""),
                "gap_height": gap.get("height", ""),
            },
        )

    matches: list[tuple[Any, dict[str, Any]]] = []
    errors: list[str] = []
    for simple_target in (False, True):
        try:
            matches.append(
                (
                    captcha_provider.match_slider(
                        target_image,
                        background_image,
                        simple_target=simple_target,
                    ),
                    {
                        "match_method": "ddddocr_match_slider_best_of_modes",
                        "simple_target": simple_target,
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"simple_target={simple_target}: {exc}")
    if matches:
        slider_match, metadata = max(
            matches,
            key=lambda item: int(getattr(item[0], "target_x", 0)),
        )
        if errors:
            metadata = {**metadata, "match_errors": " | ".join(errors)}
        return slider_match, metadata
    raise TikTokProductExtractionError("TikTok slider OCR matching failed: " + " | ".join(errors))


def _detect_tiktok_slider_gap_from_background(background_image: bytes) -> dict[str, int]:
    if not background_image:
        return {}
    try:
        from PIL import Image

        with Image.open(BytesIO(background_image)).convert("RGB") as image:
            width, height = int(image.width), int(image.height)
            raw_pixels = image.tobytes()
    except Exception:
        return {}

    if width <= 0 or height <= 0:
        return {}

    dark_mask = bytearray(
        1 if (raw_pixels[index] + raw_pixels[index + 1] + raw_pixels[index + 2]) / 3 < 95 else 0
        for index in range(0, len(raw_pixels), 3)
    )
    seen = bytearray(width * height)
    candidates: list[dict[str, int]] = []

    for index, is_dark in enumerate(dark_mask):
        if not is_dark or seen[index]:
            continue
        stack = [index]
        seen[index] = 1
        area = 0
        min_x = width
        min_y = height
        max_x = 0
        max_y = 0
        while stack:
            current = stack.pop()
            area += 1
            x = current % width
            y = current // width
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x)
            max_y = max(max_y, y)
            for neighbor_y in range(max(0, y - 1), min(height, y + 2)):
                row_offset = neighbor_y * width
                for neighbor_x in range(max(0, x - 1), min(width, x + 2)):
                    neighbor_index = row_offset + neighbor_x
                    if seen[neighbor_index] or not dark_mask[neighbor_index]:
                        continue
                    seen[neighbor_index] = 1
                    stack.append(neighbor_index)

        box_width = max_x - min_x + 1
        box_height = max_y - min_y + 1
        if not (
            35 <= box_width <= 100
            and 30 <= box_height <= 100
            and area >= 500
            and min_x > 5
            and max_x < width - 5
            and min_y > 5
            and max_y < height - 5
        ):
            continue
        candidates.append(
            {
                "x": min_x,
                "y": min_y,
                "width": box_width,
                "height": box_height,
                "area": area,
            }
        )

    if not candidates:
        return {}
    return max(candidates, key=lambda item: item["area"])


def _read_tiktok_slider_captcha_state(page: Any) -> dict[str, Any]:
    success_locator, success_selector = _first_visible_locator(
        page,
        TIKTOK_SLIDER_CAPTCHA_SUCCESS_SELECTORS,
        timeout_ms=250,
    )
    popup_locator, popup_selector = _first_visible_locator(
        page,
        TIKTOK_SLIDER_CAPTCHA_POPUP_SELECTORS,
        timeout_ms=250,
    )
    if popup_locator:
        return {
            "visible": True,
            "success": bool(success_locator),
            "selector": popup_selector,
            "success_selector": success_selector,
        }

    background_locator, background_selector = _first_visible_locator(
        page,
        TIKTOK_SLIDER_CAPTCHA_BACKGROUND_SELECTORS,
        timeout_ms=250,
    )
    handle_locator, handle_selector = _first_visible_locator(
        page,
        TIKTOK_SLIDER_CAPTCHA_HANDLE_SELECTORS,
        timeout_ms=250,
    )
    return {
        "visible": bool(background_locator and handle_locator),
        "success": bool(success_locator),
        "selector": background_selector if background_locator else "",
        "handle_selector": handle_selector if handle_locator else "",
        "success_selector": success_selector,
    }


def _wait_for_tiktok_slider_captcha_state(
    page: Any,
    *,
    timeout_ms: int = DEFAULT_TIKTOK_SLIDER_CAPTCHA_APPEAR_TIMEOUT_MS,
    poll_ms: int = DEFAULT_SECURITY_CHECK_POLL_MS,
) -> dict[str, Any]:
    effective_timeout_ms = max(int(timeout_ms), 0)
    effective_poll_ms = max(int(poll_ms), 1)
    deadline = time.monotonic() + effective_timeout_ms / 1000.0

    while True:
        state = _read_tiktok_slider_captcha_state(page)
        if state.get("visible") or time.monotonic() >= deadline:
            return state
        _safe_wait_for_timeout(page, min(effective_poll_ms, max(int((deadline - time.monotonic()) * 1000), 0)))


def _first_visible_locator(
    page: Any,
    selectors: tuple[str, ...],
    *,
    timeout_ms: int = 500,
) -> tuple[Any | None, str]:
    for selector in selectors:
        if not selector:
            continue
        try:
            locator = page.locator(selector)
            target = getattr(locator, "first", locator)
            if _locator_is_visible(target, timeout_ms=timeout_ms):
                return target, selector
        except Exception:
            continue
    return None, ""


def _locator_is_visible(locator: Any, *, timeout_ms: int = 500) -> bool:
    is_visible = getattr(locator, "is_visible", None)
    if not callable(is_visible):
        return False
    try:
        return bool(is_visible(timeout=timeout_ms))
    except TypeError:
        try:
            return bool(is_visible())
        except Exception:
            return False
    except Exception:
        return False


def _click_first_visible_locator(page: Any, selectors: tuple[str, ...]) -> bool:
    locator, _selector = _first_visible_locator(page, selectors, timeout_ms=250)
    if not locator:
        return False
    click = getattr(locator, "click", None)
    if not callable(click):
        return False
    try:
        click(timeout=1000)
    except TypeError:
        click()
    return True


def _locator_screenshot_bytes(locator: Any) -> bytes:
    screenshot = getattr(locator, "screenshot", None)
    if not callable(screenshot):
        return b""
    try:
        payload = screenshot(timeout=3000)
    except TypeError:
        payload = screenshot()
    return payload if isinstance(payload, bytes) else b""


def _hide_locator_for_visual_capture(locator: Any) -> bool:
    evaluate = getattr(locator, "evaluate", None)
    if not callable(evaluate):
        return False
    script = """
    element => {
      if (!element || !element.style) return false;
      element.dataset.tiktokSliderPreviousVisibility = element.style.visibility || "";
      element.style.visibility = "hidden";
      return true;
    }
    """
    try:
        return bool(evaluate(script, timeout=1000))
    except TypeError:
        try:
            return bool(evaluate(script))
        except Exception:
            return False
    except Exception:
        return False


def _restore_locator_after_visual_capture(locator: Any) -> None:
    evaluate = getattr(locator, "evaluate", None)
    if not callable(evaluate):
        return
    script = """
    element => {
      if (!element || !element.style || !element.dataset) return;
      element.style.visibility = element.dataset.tiktokSliderPreviousVisibility || "";
      delete element.dataset.tiktokSliderPreviousVisibility;
    }
    """
    try:
        evaluate(script, timeout=1000)
    except TypeError:
        try:
            evaluate(script)
        except Exception:
            return
    except Exception:
        return


def _locator_bounding_box(locator: Any) -> dict[str, float]:
    bounding_box = getattr(locator, "bounding_box", None)
    if not callable(bounding_box):
        return {}
    try:
        box = bounding_box(timeout=3000)
    except TypeError:
        box = bounding_box()
    if not isinstance(box, Mapping):
        return {}
    try:
        return {
            "x": float(box.get("x", 0)),
            "y": float(box.get("y", 0)),
            "width": float(box.get("width", 0)),
            "height": float(box.get("height", 0)),
        }
    except (TypeError, ValueError):
        return {}


def _calculate_tiktok_slider_drag_distance(
    *,
    slider_match: Any,
    background_image: bytes,
    background_box: Mapping[str, float],
    target_box: Mapping[str, float],
    handle_box: Mapping[str, float],
) -> float:
    background_render_width = float(background_box.get("width") or 0)
    image_width, _image_height = _image_dimensions_from_bytes(background_image)
    coordinate_scale = background_render_width / image_width if image_width > 0 else 1.0
    target_left = float(getattr(slider_match, "target_x", 0)) * coordinate_scale
    raw_match = getattr(slider_match, "raw", None)
    if isinstance(raw_match, Mapping) and raw_match.get("method") == "dark_gap_component":
        try:
            gap_width = float(raw_match.get("width") or 0) * coordinate_scale
        except (TypeError, ValueError):
            gap_width = 0.0
        target_render_width = float(target_box.get("width") or 0) if target_box else 0.0
        if gap_width > 0 and target_render_width > gap_width:
            target_left -= (target_render_width - gap_width) / 2

    if target_box:
        current_left = float(target_box.get("x") or 0) - float(background_box.get("x") or 0)
    else:
        current_left = float(handle_box.get("x") or 0) - float(background_box.get("x") or 0)
    drag_distance = target_left - current_left
    if abs(drag_distance) < 1:
        drag_distance = target_left
    return drag_distance


def _image_dimensions_from_bytes(image_bytes: bytes) -> tuple[int, int]:
    if not image_bytes:
        return 0, 0
    try:
        from PIL import Image

        with Image.open(BytesIO(image_bytes)) as image:
            return int(image.width), int(image.height)
    except Exception:
        return 0, 0


def _drag_tiktok_slider_handle(
    page: Any,
    *,
    handle_box: Mapping[str, float],
    drag_distance: float,
) -> None:
    mouse = getattr(page, "mouse", None)
    if mouse is None:
        raise TikTokProductExtractionError("TikTok slider captcha requires page mouse support")

    start_x = float(handle_box.get("x") or 0) + float(handle_box.get("width") or 0) / 2
    start_y = float(handle_box.get("y") or 0) + float(handle_box.get("height") or 0) / 2
    end_x = start_x + drag_distance
    overshoot = 0.0
    if abs(drag_distance) >= 80:
        overshoot = min(max(abs(drag_distance) * 0.025, 2.0), 8.0)
        overshoot = overshoot if drag_distance >= 0 else -overshoot
    travel_distance = drag_distance + overshoot
    steps = max(32, min(56, int(abs(travel_distance) // 5) or 32))
    step_pause_ms = max(8, min(24, int(900 / steps)))

    mouse.move(start_x, start_y)
    _safe_wait_for_timeout(page, random.randint(100, 240))
    mouse.down()
    _safe_wait_for_timeout(page, random.randint(140, 280))
    for step in range(1, steps + 1):
        progress = step / steps
        if progress < 0.75:
            eased = 1 - (1 - progress) ** 3
        else:
            eased = 0.98 + (progress - 0.75) * 0.08
        eased = min(eased, 1.0)
        jitter_x = 0.0 if step == steps else random.uniform(-0.6, 0.6)
        jitter_y = 0.0 if step == steps else random.uniform(-1.2, 1.2)
        mouse.move(start_x + travel_distance * eased + jitter_x, start_y + jitter_y)
        _safe_wait_for_timeout(page, random.randint(max(5, step_pause_ms - 4), step_pause_ms + 8))
    if overshoot:
        mouse.move(end_x + overshoot * 0.35, start_y + random.uniform(-0.4, 0.4))
        _safe_wait_for_timeout(page, random.randint(80, 160))
        mouse.move(end_x - (1 if drag_distance >= 0 else -1) * random.uniform(0.4, 1.4), start_y)
        _safe_wait_for_timeout(page, random.randint(70, 140))
    mouse.move(end_x, start_y)
    _safe_wait_for_timeout(page, random.randint(120, 260))
    mouse.up()


def _is_tiktok_login_promo_blocker(event: BlockedContext) -> bool:
    page_url = str(getattr(event, "page_url", "") or "").lower()
    blocker_type = str(getattr(event, "blocker_type", "") or "").strip().lower()
    if "tiktok.com/shop/pdp/" not in page_url or blocker_type not in {"guide_overlay", "dom_modal", "unknown"}:
        return False

    candidate_texts = _collect_tiktok_blocked_text_candidates(event)
    if not any(_contains_login_prompt(text) for text in candidate_texts):
        return False
    if any("create account" in text for text in candidate_texts):
        return True
    if any(keyword in text for text in candidate_texts for keyword in TIKTOK_LOGIN_PROMO_KEYWORDS):
        return True
    return any(marker in text for text in candidate_texts for marker in TIKTOK_EARLY_LOGIN_PROMO_MARKERS)


def _collect_tiktok_blocked_text_candidates(event: BlockedContext) -> tuple[str, ...]:
    candidates: list[str] = []
    normalized_summary = _normalize_browser_text(getattr(event, "summary", ""))
    if normalized_summary:
        candidates.append(normalized_summary)

    dom_summary = getattr(event, "dom_summary", None)
    if isinstance(dom_summary, dict):
        dialogs = dom_summary.get("dialogs")
        if isinstance(dialogs, list):
            for item in dialogs:
                if not isinstance(item, dict):
                    continue
                normalized_dialog_text = _normalize_browser_text(item.get("text", ""))
                if normalized_dialog_text:
                    candidates.append(normalized_dialog_text)
        normalized_body_text = _normalize_browser_text(dom_summary.get("body_text_excerpt", ""))
        if normalized_body_text:
            candidates.append(normalized_body_text)

    deduped: list[str] = []
    seen: set[str] = set()
    for text in candidates:
        if text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return tuple(deduped)


def _contains_login_prompt(text: str) -> bool:
    return any(token in text for token in ("log in", "login", "sign in", "signin"))


def _dismiss_tiktok_login_promo(page: Any) -> bool:
    if not _read_tiktok_login_promo_state(page).get("visible"):
        return False

    _wait_with_random_delay(
        page,
        min_ms=DEFAULT_TIKTOK_BLOCKER_PRE_DISMISS_MIN_MS,
        max_ms=DEFAULT_TIKTOK_BLOCKER_PRE_DISMISS_MAX_MS,
    )

    keyboard = getattr(page, "keyboard", None)
    if keyboard is not None and hasattr(keyboard, "press"):
        try:
            keyboard.press("Escape")
            _wait_with_random_delay(
                page,
                min_ms=DEFAULT_TIKTOK_BLOCKER_SETTLE_MIN_MS,
                max_ms=DEFAULT_TIKTOK_BLOCKER_SETTLE_MAX_MS,
            )
        except Exception:
            pass
        if not _read_tiktok_login_promo_state(page).get("visible"):
            return True

    mouse = getattr(page, "mouse", None)
    if mouse is not None and hasattr(mouse, "click"):
        try:
            _wait_with_random_delay(
                page,
                min_ms=DEFAULT_TIKTOK_BLOCKER_RETRY_MIN_MS,
                max_ms=DEFAULT_TIKTOK_BLOCKER_RETRY_MAX_MS,
            )
            mouse.click(40, 40)
            _wait_with_random_delay(
                page,
                min_ms=DEFAULT_TIKTOK_BLOCKER_SETTLE_MIN_MS,
                max_ms=DEFAULT_TIKTOK_BLOCKER_SETTLE_MAX_MS,
            )
        except Exception:
            pass
        if not _read_tiktok_login_promo_state(page).get("visible"):
            return True

    return False


def _tiktok_product_content_is_visible(page: Any) -> bool:
    try:
        dom_snapshot = _read_dom_product_snapshot(page)
    except Exception:
        return False
    return int(dom_snapshot.get("visible_signal_count", 0)) >= 2


def _read_tiktok_login_promo_state(page: Any) -> dict[str, Any]:
    try:
        payload = page.evaluate(
            """(args) => {
                const selectors = args.selectors || [];
                const keywords = (args.keywords || [])
                  .map((value) => String(value || "").replace(/\\s+/g, " ").trim().toLowerCase())
                  .filter(Boolean);

                const normalizeText = (value) => String(value || "")
                  .replace(/\\s+/g, " ")
                  .trim();

                const isVisible = (element) => {
                  if (!element) return false;
                  const rect = element.getBoundingClientRect();
                  const style = window.getComputedStyle(element);
                  return rect.width >= 120 && rect.height >= 60 &&
                    style.visibility !== "hidden" &&
                    style.display !== "none" &&
                    Number(style.opacity || "1") !== 0;
                };

                for (const selector of selectors) {
                  for (const element of document.querySelectorAll(selector)) {
                    if (!isVisible(element)) continue;
                    const text = normalizeText(element.innerText || element.textContent || "");
                    const loweredText = text.toLowerCase();
                    if (!keywords.every((keyword) => loweredText.includes(keyword))) continue;
                    return {
                      visible: true,
                      text,
                      selector,
                    };
                  }
                }

                return {
                  visible: false,
                  text: "",
                  selector: "",
                };
            }""",
            {
                "selectors": ["[role='dialog']", "[aria-modal='true']", "dialog", "[class*='popover']"],
                "keywords": ["log in", "create account"],
            },
        )
    except Exception:
        return {
            "visible": False,
            "text": "",
            "selector": "",
        }
    return payload if isinstance(payload, dict) else {}


def _read_dom_product_snapshot(page: Any) -> dict[str, Any]:
    payload = page.evaluate(
        r"""(args) => {
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

            const normalizeText = (value) => String(value || "").replace(/\s+/g, " ").trim();

            const pickVisiblePriceText = () => {
              const marker = "data-mujitask-price-fallback";
              for (const previous of document.querySelectorAll(`[${marker}]`)) {
                previous.removeAttribute(marker);
              }

              const candidates = [];
              for (const element of document.querySelectorAll("body *")) {
                if (!isVisible(element)) continue;
                const text = normalizeText(element.textContent);
                if (!text || text.length > 80 || !/\$\s*\d/.test(text)) continue;
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                const textDecoration = String(style.textDecorationLine || style.textDecoration || "");
                const isStruck = textDecoration.includes("line-through");
                const hasActionText = /add to cart|buy now|unlock price/i.test(text);
                const hasLetters = /[A-Za-z]{3,}/.test(text);
                const compactLengthPenalty = Math.max(text.replace(/\s+/g, "").length - 10, 0);
                candidates.push({
                  text,
                  element,
                  y: rect.top,
                  x: rect.left,
                  score:
                    (isStruck ? 1000 : 0) +
                    (hasActionText ? 300 : 0) +
                    (hasLetters ? 150 : 0) +
                    compactLengthPenalty +
                    Math.max(rect.top, 0) / 1000,
                });
              }
              candidates.sort((left, right) => {
                if (left.score !== right.score) return left.score - right.score;
                if (left.y !== right.y) return left.y - right.y;
                return left.x - right.x;
              });
              const picked = candidates[0];
              if (!picked) return { text: "", selector: "" };
              picked.element.setAttribute(marker, "1");
              return { text: picked.text, selector: `[${marker}="1"]` };
            };

            const pickImage = (items) => {
              for (const selector of items) {
                const element = document.querySelector(selector);
                if (!(element instanceof HTMLImageElement) || !isVisible(element)) continue;
                const src = (element.currentSrc || element.src || "").trim();
                if (!src || imageLooksLikeUtility(src)) continue;
                return {
                  src,
                  selector,
                  loaded: Boolean(element.complete && element.naturalWidth > 0),
                };
              }
              return { src: "", selector: "", loaded: false };
            };

            const imageLooksLikeUtility = (src) => {
              const lowered = String(src || "").toLowerCase();
              return !lowered ||
                lowered.startsWith("data:") ||
                lowered.includes("logo") ||
                lowered.includes("avatar") ||
                lowered.includes("sprite") ||
                lowered.includes("icon") ||
                lowered.includes("tiktok_shop_web_mono");
            };

            const pickVisibleProductImage = () => {
              const marker = "data-mujitask-main-image-fallback";
              for (const previous of document.querySelectorAll(`[${marker}]`)) {
                previous.removeAttribute(marker);
              }

              const candidates = collectVisibleProductImages({ minSize: 120 });
              const picked = candidates[0];
              if (!picked) return { src: "", selector: "", loaded: false };
              picked.element.setAttribute(marker, "1");
              return {
                src: picked.src,
                selector: `[${marker}="1"]`,
                loaded: picked.loaded,
              };
            };

            const collectVisibleProductImages = ({ minSize = 64 } = {}) => {
              const candidates = [];
              const seen = new Set();
              for (const element of document.querySelectorAll("img")) {
                if (!(element instanceof HTMLImageElement) || !isVisible(element)) continue;
                const src = (element.currentSrc || element.src || "").trim();
                if (!src || imageLooksLikeUtility(src)) continue;
                if (seen.has(src)) continue;
                seen.add(src);
                const rect = element.getBoundingClientRect();
                const viewportLeft = Math.max(rect.left, 0);
                const viewportTop = Math.max(rect.top, 0);
                const viewportRight = Math.min(rect.right, window.innerWidth);
                const viewportBottom = Math.min(rect.bottom, window.innerHeight);
                const viewportWidth = Math.max(viewportRight - viewportLeft, 0);
                const viewportHeight = Math.max(viewportBottom - viewportTop, 0);
                const viewportArea = viewportWidth * viewportHeight;
                const width = Math.max(rect.width, element.naturalWidth || 0);
                const height = Math.max(rect.height, element.naturalHeight || 0);
                if (width < minSize || height < minSize || viewportWidth < minSize || viewportHeight < minSize) continue;
                const hintText = `${src} ${element.alt || ""}`;
                const hasProductHint = /product|pdp|oec|tos|byteimg|tiktokcdn/i.test(hintText);
                const rightSidePenalty = rect.left > window.innerWidth * 0.45 ? 50000 : 0;
                const belowFoldPenalty = rect.top > window.innerHeight ? 100000 : 0;
                const carouselCurrentBonus =
                  element.closest(".slick-current,.slick-active,[aria-current='true']") ? 100000 : 0;
                candidates.push({
                  element,
                  src,
                  loaded: Boolean(element.complete && element.naturalWidth > 0),
                  y: rect.top,
                  x: rect.left,
                  score:
                    -viewportArea -
                    (hasProductHint ? 50000 : 0) -
                    carouselCurrentBonus +
                    rightSidePenalty +
                    belowFoldPenalty +
                    Math.max(rect.top, 0),
                });
              }
              candidates.sort((left, right) => {
                if (left.score !== right.score) return left.score - right.score;
                if (left.y !== right.y) return left.y - right.y;
                return left.x - right.x;
              });
              return candidates;
            };

            const parseCompactNumber = (value) => {
              const raw = normalizeText(value).replace(/,/g, "");
              const match = raw.match(/([0-9]+(?:\.[0-9]+)?)/);
              if (!match) return 0;
              let number = Number(match[1]);
              if (!Number.isFinite(number)) return 0;
              if (/[Kk]/.test(raw)) number *= 1000;
              if (/万/.test(raw)) number *= 10000;
              return Math.round(number);
            };

            const pickVisibleReviewMetrics = () => {
              const text = normalizeText(document.body ? document.body.innerText : "");
              const metrics = {
                rating_score: 0,
                review_count: 0,
                comment_count: 0,
                sales_count: 0,
              };
              const reviewMatch =
                text.match(/([0-5](?:\.[0-9])?)\s*\(?\s*([0-9][0-9,.Kk万]*)\s*\)?\s*(?:global\s*)?(?:reviews?|ratings?|评价|评论)/i) ||
                text.match(/([0-5](?:\.[0-9])?)\s*\(\s*([0-9][0-9,.Kk万]*)\s*\)/);
              if (reviewMatch) {
                metrics.rating_score = Number(reviewMatch[1]) || 0;
                metrics.review_count = parseCompactNumber(reviewMatch[2]);
                metrics.comment_count = metrics.review_count;
              }
              const salesMatch =
                text.match(/([0-9][0-9,.Kk万]*)\s*(?:sold|已售)/i) ||
                text.match(/(?:sold|已售)\s*([0-9][0-9,.Kk万]*)/i);
              if (salesMatch) metrics.sales_count = parseCompactNumber(salesMatch[1]);
              return metrics;
            };

            const elementTextLines = (element) => String(
              element && (element.innerText || element.textContent || "") || ""
            )
              .split(/\n+/)
              .map((part) => normalizeText(part))
              .filter(Boolean);

            const cleanSkuValue = (value, optionName = "") => {
              const escapeRegex = (text) => String(text || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
              let text = normalizeText(value)
                .replace(/\$\s*[0-9][0-9,.]*/g, " ")
                .replace(/unlock price|sold out|out of stock|add to cart|buy now/ig, " ");
              if (optionName) {
                text = text.replace(new RegExp(`^${escapeRegex(optionName)}\\s*[:：]?\\s*`, "i"), "");
              }
              const parts = text
                .split(/\s+/)
                .map((part) => part.trim())
                .filter(Boolean);
              if (parts.length > 1 && parts.every((part) => part.toLowerCase() === parts[0].toLowerCase())) {
                return parts[0];
              }
              return normalizeText(parts.join(" "));
            };

            const looksLikeSkuValue = (value) => {
              const text = normalizeText(value);
              if (!text || text.length > 40) return false;
              return !/coupon|center|login|search|quantity|shipping|sold|reviews?|ratings?|unlock|price|cart/i.test(text);
            };

            const nearestOptionLabel = (element) => {
              const rect = element.getBoundingClientRect();
              const labels = [];
              for (const candidate of document.querySelectorAll("body *")) {
                if (!isVisible(candidate)) continue;
                const text = normalizeText(candidate.textContent);
                const match = text.match(/^([^:：]{1,32})[:：]\s*([^:：]{0,50})$/);
                if (!match) continue;
                if (/shipping|sold|reviews?|ratings?|price/i.test(match[1])) continue;
                const labelRect = candidate.getBoundingClientRect();
                if (labelRect.bottom > rect.top + 6) continue;
                if (rect.top - labelRect.bottom > 180) continue;
                if (Math.abs(labelRect.left - rect.left) > 80 && labelRect.left > rect.left) continue;
                labels.push({
                  name: normalizeText(match[1]),
                  selectedValue: cleanSkuValue(match[2], match[1]),
                  distance: rect.top - labelRect.bottom + Math.abs(labelRect.left - rect.left) / 10,
                });
              }
              labels.sort((left, right) => left.distance - right.distance);
              return labels[0] || { name: "", selectedValue: "" };
            };

            const skuCardForImage = (image) => {
              let current = image;
              let best = null;
              for (let depth = 0; depth < 6 && current; depth += 1) {
                if (!isVisible(current)) {
                  current = current.parentElement;
                  continue;
                }
                const rect = current.getBoundingClientRect();
                const lines = elementTextLines(current);
                const shortLines = lines.filter((line) => looksLikeSkuValue(line));
                const hasPointer =
                  current.tagName === "BUTTON" ||
                  current.getAttribute("role") === "button" ||
                  current.getAttribute("tabindex") !== null ||
                  window.getComputedStyle(current).cursor === "pointer";
                if (rect.width >= 36 && rect.height >= 36 && rect.width <= 260 && rect.height <= 260 && shortLines.length) {
                  best = { element: current, lines: shortLines, hasPointer };
                  if (hasPointer) break;
                }
                current = current.parentElement;
              }
              return best;
            };

            const collectVisibleSkuOptions = () => {
              const rawOptions = [];
              const seen = new Set();
              for (const imageElement of document.querySelectorAll("img")) {
                if (!(imageElement instanceof HTMLImageElement) || !isVisible(imageElement)) continue;
                const sourceUrl = (imageElement.currentSrc || imageElement.src || "").trim();
                if (!sourceUrl || imageLooksLikeUtility(sourceUrl)) continue;
                const imageRect = imageElement.getBoundingClientRect();
                if (imageRect.width < 32 || imageRect.height < 32 || imageRect.width > 220 || imageRect.height > 220) {
                  continue;
                }
                const card = skuCardForImage(imageElement);
                if (!card) continue;
                const label = nearestOptionLabel(card.element);
                if (!label.name) continue;
                const valueCandidates = [
                  ...card.lines,
                  normalizeText(imageElement.alt),
                  label.selectedValue,
                ].map((value) => cleanSkuValue(value, label.name)).filter(looksLikeSkuValue);
                if (!valueCandidates.length) continue;
                const optionValue = valueCandidates.sort((left, right) => left.length - right.length)[0];
                const skuPropertyKey = `${label.name}:${optionValue}`;
                const dedupeKey = `${skuPropertyKey}|${sourceUrl}`;
                if (seen.has(dedupeKey)) continue;
                seen.add(dedupeKey);
                const rect = card.element.getBoundingClientRect();
                rawOptions.push({
                  option_name: label.name,
                  option_value: optionValue,
                  sku_property_key: skuPropertyKey,
                  source_url: sourceUrl,
                  display_order: rawOptions.length,
                  selected: Boolean(label.selectedValue) &&
                    cleanSkuValue(label.selectedValue, label.name).toLowerCase() === optionValue.toLowerCase(),
                  x: rect.left,
                  y: rect.top,
                });
              }
              rawOptions.sort((left, right) => {
                if (left.option_name !== right.option_name) return left.option_name.localeCompare(right.option_name);
                if (left.y !== right.y) return left.y - right.y;
                return left.x - right.x;
              });
              return rawOptions.map((item, index) => ({ ...item, display_order: index }));
            };

            const collectVisibleTextSkuOptions = (imageOptions = []) => {
              const rawOptions = [];
              const seen = new Set(
                imageOptions.map((item) => `${item.option_name}:${item.option_value}`.toLowerCase())
              );
              const candidates = Array.from(document.querySelectorAll("button,[role='button'],[tabindex]"))
                .filter((element) => isVisible(element))
                .filter((element) => !element.querySelector("img"));
              for (const element of candidates) {
                const rect = element.getBoundingClientRect();
                if (rect.width < 32 || rect.height < 24 || rect.width > 260 || rect.height > 120) continue;
                const label = nearestOptionLabel(element);
                if (!label.name) continue;
                const valueCandidates = elementTextLines(element)
                  .map((value) => cleanSkuValue(value, label.name))
                  .filter(looksLikeSkuValue);
                if (!valueCandidates.length) continue;
                const optionValue = valueCandidates.sort((left, right) => left.length - right.length)[0];
                if (label.name.toLowerCase() === "quantity" && /^(?:[+\-]|\d+)$/.test(optionValue)) {
                  continue;
                }
                const dedupeKey = `${label.name}:${optionValue}`.toLowerCase();
                if (seen.has(dedupeKey)) continue;
                seen.add(dedupeKey);
                rawOptions.push({
                  option_name: label.name,
                  option_value: optionValue,
                  sku_property_key: `${label.name}:${optionValue}`,
                  source_url: "",
                  display_order: rawOptions.length,
                  selected: Boolean(label.selectedValue) &&
                    cleanSkuValue(label.selectedValue, label.name).toLowerCase() === optionValue.toLowerCase(),
                  x: rect.left,
                  y: rect.top,
                });
              }
              rawOptions.sort((left, right) => {
                if (left.option_name !== right.option_name) return left.option_name.localeCompare(right.option_name);
                if (left.y !== right.y) return left.y - right.y;
                return left.x - right.x;
              });
              return rawOptions.map((item, index) => ({ ...item, display_order: index }));
            };

            const buildSkuOptionGroups = (skuOptions) => {
              const groups = new Map();
              for (const item of skuOptions) {
                if (!groups.has(item.option_name)) {
                  groups.set(item.option_name, {
                    name: item.option_name,
                    values: [],
                    source_platform: "tiktok",
                  });
                }
                const group = groups.get(item.option_name);
                if (group.values.some((value) => value.value.toLowerCase() === item.option_value.toLowerCase())) {
                  continue;
                }
                group.values.push({
                  value: item.option_value,
                  image_url: item.source_url || "",
                  sku_property_key: item.sku_property_key,
                  selected: item.selected,
                });
              }
              return Array.from(groups.values());
            };

            const buildDomSkus = (skuOptions) => {
              if (skuOptions.length !== 1) return [];
              const option = skuOptions[0];
              return option.values.map((value) => ({
                product_id: "",
                sku_id: "",
                sku_name: value.value,
                spec_name: `${option.name}: ${value.value}`,
                properties: [{
                  name: option.name,
                  value: value.value,
                  sku_property_key: value.sku_property_key,
                  image_url: value.image_url,
                }],
                sku_property_keys: [value.sku_property_key],
                source_platform: "tiktok",
              }));
            };

            const title = pickText(selectors.title);
            let price = pickText(selectors.price);
            if (!price.text) price = pickVisiblePriceText();
            const shop = pickText(selectors.shop);
            let image = pickImage(selectors.image);
            if (!image.src) image = pickVisibleProductImage();
            const reviewMetrics = pickVisibleReviewMetrics();
            const skuImages = collectVisibleSkuOptions();
            const textSkuOptions = collectVisibleTextSkuOptions(skuImages);
            const skuOptions = buildSkuOptionGroups([...skuImages, ...textSkuOptions]);
            const galleryImageUrls = collectVisibleProductImages({ minSize: 64 })
              .map((candidate) => candidate.src)
              .filter((src) => src && src !== image.src)
              .slice(0, 12);

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
              gallery_image_urls: galleryImageUrls,
              sku_images: skuImages,
              sku_options: skuOptions,
              skus: buildDomSkus(skuOptions),
              rating_score: reviewMetrics.rating_score,
              review_count: reviewMetrics.review_count,
              comment_count: reviewMetrics.comment_count,
              sales_count: reviewMetrics.sales_count,
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


def _extract_unavailable_message(text: str) -> str | None:
    normalized_text = _normalize_browser_text(text)
    if not normalized_text:
        return None

    for needle, display in UNAVAILABLE_PAGE_SIGNALS:
        if needle in normalized_text:
            return f"TikTok product unavailable: {display}"
    return None


def _normalize_browser_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


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


def _extract_product_gallery_images(product_model: dict[str, Any]) -> list[dict[str, Any]]:
    images = product_model.get("images")
    if not isinstance(images, list):
        return []

    gallery_images: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for display_order, image in enumerate(images):
        if not isinstance(image, dict):
            continue
        url = _pick_url_from_media(image)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        gallery_images.append(
            _media_reference_from_node(
                image,
                source_url=url,
                display_order=display_order,
                media_role="product_gallery_image",
            )
        )
    return gallery_images


def _extract_product_sku_images(product_model: dict[str, Any]) -> list[dict[str, Any]]:
    sku_property_image_map = product_model.get("sku_property_image_map")
    if not isinstance(sku_property_image_map, dict):
        return []

    sku_images: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for display_order, (sku_property_key, image) in enumerate(sku_property_image_map.items()):
        if not isinstance(image, dict):
            continue
        url = _pick_url_from_media(image)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        media_reference = _media_reference_from_node(
            image,
            source_url=url,
            display_order=display_order,
            media_role="product_sku_image",
        )
        media_reference["sku_property_key"] = str(sku_property_key)
        sku_images.append(media_reference)
    return sku_images


def _extract_product_sku_options(*payloads: Any) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        image_map = payload.get("sku_property_image_map")
        if isinstance(image_map, dict):
            for sku_property_key, image in image_map.items():
                prop_name, prop_value = _split_sku_property_key(sku_property_key)
                if prop_name and prop_value:
                    _add_sku_option(
                        groups,
                        {
                            "name": prop_name,
                            "value": prop_value,
                            "image_url": _pick_url_from_media(image),
                            "sku_property_key": f"{prop_name}:{prop_value}",
                        },
                    )

        for node in _walk_json(payload):
            if not isinstance(node, dict):
                continue
            for prop in _sku_prop_assignments(node):
                _add_sku_option(groups, prop)
            group_name = _sku_group_name(node)
            values = _sku_group_values(node)
            if not group_name or not values:
                continue
            for value_node in values:
                value = _sku_option_value(value_node)
                if not value:
                    continue
                _add_sku_option(
                    groups,
                    {
                        "name": group_name,
                        "value": value,
                        "value_id": _sku_option_value_id(value_node),
                        "image_url": _pick_url_from_media(value_node),
                        "sku_property_key": f"{group_name}:{value}",
                    },
                )
    return list(groups.values())


def _extract_product_skus(
    *payloads: Any,
    sku_options: list[dict[str, Any]],
    product_id: str,
) -> list[dict[str, Any]]:
    skus: list[dict[str, Any]] = []
    for row in _iter_sku_rows(payloads):
        sku = _sku_from_row(row, product_id=product_id)
        if sku:
            skus.append(sku)

    if not skus and len(sku_options) == 1:
        option = sku_options[0]
        option_name = _text_value(option.get("name"))
        for value in option.get("values") or []:
            if not isinstance(value, dict):
                continue
            option_value = _text_value(value.get("value"))
            if not option_name or not option_value:
                continue
            property_pair = {
                "name": option_name,
                "value": option_value,
                "value_id": _text_value(value.get("value_id")),
                "sku_property_key": _text_value(value.get("sku_property_key")) or f"{option_name}:{option_value}",
            }
            skus.append(
                {
                    "product_id": product_id,
                    "sku_id": "",
                    "sku_name": option_value,
                    "spec_name": f"{option_name}: {option_value}",
                    "properties": [property_pair],
                    "sku_property_keys": [property_pair["sku_property_key"]],
                    "source_platform": "tiktok",
                }
            )
    return _dedupe_tiktok_skus(skus)


def _iter_sku_rows(payloads: tuple[Any, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for payload in payloads:
        for node in _walk_json(payload):
            if not isinstance(node, dict):
                continue
            for key in (
                "sku_list",
                "skus",
                "sku_infos",
                "sku_items",
                "product_skus",
                "sku_info_list",
            ):
                value = node.get(key)
                if isinstance(value, list):
                    rows.extend(dict(item) for item in value if isinstance(item, dict))
                elif isinstance(value, dict):
                    rows.extend(dict(item) for item in value.values() if isinstance(item, dict))
    return rows


def _sku_from_row(row: dict[str, Any], *, product_id: str) -> dict[str, Any]:
    props = _sku_row_properties(row)
    sku_id = _text_value(row.get("sku_id") or row.get("id") or row.get("skuId"))
    sku_name = _text_value(
        row.get("sku_name")
        or row.get("name")
        or row.get("skuName")
        or _join_sku_prop_values(props)
        or sku_id
    )
    if not (sku_id or sku_name or props):
        return {}
    spec_name = _join_sku_prop_pairs(props) or sku_name
    result = {
        "product_id": _text_value(row.get("product_id") or row.get("productId")) or product_id,
        "sku_id": sku_id,
        "sku_name": sku_name,
        "spec_name": spec_name,
        "properties": props,
        "sku_property_keys": [prop["sku_property_key"] for prop in props if prop.get("sku_property_key")],
        "source_platform": "tiktok",
    }
    for source_key, target_key in (
        ("real_price", "price_text"),
        ("format_price", "price_text"),
        ("price", "price_text"),
        ("sale_price", "price_text"),
        ("real_price_value", "price_amount"),
        ("price_amount", "price_amount"),
        ("stock", "stock_count"),
        ("stock_count", "stock_count"),
    ):
        value = row.get(source_key)
        if value not in (None, "") and target_key not in result:
            result[target_key] = value
    return result


def _sku_row_properties(row: dict[str, Any]) -> list[dict[str, str]]:
    for key in (
        "sku_sale_props",
        "sale_props",
        "props",
        "properties",
        "sale_attributes",
        "sales_attributes",
        "sku_properties",
    ):
        value = row.get(key)
        props = _sku_prop_assignments(value)
        if props:
            return props
    props = _sku_prop_assignments(row)
    return props


def _sku_prop_assignments(value: Any) -> list[dict[str, str]]:
    if isinstance(value, list):
        props: list[dict[str, str]] = []
        for item in value:
            props.extend(_sku_prop_assignments(item))
        return props
    if isinstance(value, dict):
        prop_name = _text_value(
            value.get("prop_name")
            or value.get("property_name")
            or value.get("sale_prop_name")
            or value.get("sku_property_name")
            or value.get("attr_name")
        )
        prop_value = _text_value(
            value.get("prop_value")
            or value.get("value_name")
            or value.get("property_value")
            or value.get("sale_prop_value")
            or value.get("sku_value")
            or value.get("attr_value")
            or value.get("value")
        )
        if prop_name and prop_value:
            return [
                {
                    "name": prop_name,
                    "value": prop_value,
                    "value_id": _text_value(value.get("prop_value_id") or value.get("value_id")),
                    "sku_property_key": f"{prop_name}:{prop_value}",
                    "image_url": _pick_url_from_media(value),
                }
            ]
        return []
    return []


def _sku_group_name(node: dict[str, Any]) -> str:
    return _text_value(
        node.get("prop_name")
        or node.get("property_name")
        or node.get("sale_prop_name")
        or node.get("sku_property_name")
        or node.get("attr_name")
        or node.get("name")
    )


def _sku_group_values(node: dict[str, Any]) -> list[Any]:
    for key in (
        "sale_prop_values",
        "values",
        "value_list",
        "sku_values",
        "options",
        "property_values",
        "children",
    ):
        value = node.get(key)
        if isinstance(value, list):
            return value
    return []


def _sku_option_value(value_node: Any) -> str:
    if isinstance(value_node, dict):
        return _text_value(
            value_node.get("prop_value")
            or value_node.get("value_name")
            or value_node.get("property_value")
            or value_node.get("name")
            or value_node.get("value")
        )
    return _text_value(value_node)


def _sku_option_value_id(value_node: Any) -> str:
    if isinstance(value_node, dict):
        return _text_value(value_node.get("prop_value_id") or value_node.get("value_id") or value_node.get("id"))
    return ""


def _add_sku_option(groups: dict[str, dict[str, Any]], prop: dict[str, Any]) -> None:
    name = _text_value(prop.get("name"))
    value = _text_value(prop.get("value"))
    if not name or not value or not _looks_like_sku_property_name(name):
        return
    group_key = name.strip().lower()
    group = groups.setdefault(
        group_key,
        {
            "name": name,
            "values": [],
            "source_platform": "tiktok",
        },
    )
    values = group["values"]
    for item in values:
        if not isinstance(item, dict):
            continue
        if _text_value(item.get("value")).lower() != value.lower():
            continue
        for key in ("value_id", "image_url", "sku_property_key"):
            if not _text_value(item.get(key)) and _text_value(prop.get(key)):
                item[key] = _text_value(prop.get(key))
        return
    values.append(
        {
            "value": value,
            "value_id": _text_value(prop.get("value_id")),
            "image_url": _text_value(prop.get("image_url")),
            "sku_property_key": _text_value(prop.get("sku_property_key")) or f"{name}:{value}",
        }
    )


def _split_sku_property_key(value: Any) -> tuple[str, str]:
    text = _text_value(value)
    if ":" not in text:
        return "", ""
    name, prop_value = text.split(":", 1)
    return name.strip(), prop_value.strip()


def _join_sku_prop_values(props: list[dict[str, str]]) -> str:
    return " / ".join(_text_value(prop.get("value")) for prop in props if _text_value(prop.get("value")))


def _join_sku_prop_pairs(props: list[dict[str, str]]) -> str:
    return " / ".join(
        f"{_text_value(prop.get('name'))}: {_text_value(prop.get('value'))}"
        for prop in props
        if _text_value(prop.get("name")) and _text_value(prop.get("value"))
    )


def _dedupe_tiktok_skus(skus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sku in skus:
        key = _text_value(sku.get("sku_id")) or _text_value(sku.get("spec_name")) or _text_value(sku.get("sku_name"))
        normalized = re.sub(r"\s+", "", key.lower())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(sku)
    return deduped


def _walk_json(value: Any, *, max_depth: int = 8) -> list[Any]:
    values: list[Any] = []

    def walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            return
        values.append(node)
        if isinstance(node, dict):
            for item in node.values():
                if isinstance(item, (dict, list)):
                    walk(item, depth + 1)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    walk(item, depth + 1)

    walk(value, 0)
    return values


def _looks_like_sku_property_name(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value.strip().lower())
    return normalized not in {
        "",
        "productid",
        "productname",
        "title",
        "name",
        "image",
        "images",
        "price",
        "stock",
        "soldcount",
        "reviewcount",
    }


def _text_value(value: Any) -> str:
    return str(value or "").strip()


def _media_reference_from_node(
    media: dict[str, Any],
    *,
    source_url: str,
    display_order: int,
    media_role: str,
) -> dict[str, Any]:
    reference: dict[str, Any] = {
        "source_url": source_url,
        "display_order": display_order,
        "media_role": media_role,
    }
    for key in ("uri", "width", "height"):
        if key in media and media.get(key) not in (None, ""):
            reference[key] = media[key]
    uri = str(media.get("uri") or "").strip()
    if uri:
        reference["file_token"] = f"tiktok_uri:{uri}"
    return reference


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
    for nested_key in ("image", "cover", "origin_image", "main_image", "image_info"):
        url = _pick_url_from_media(media.get(nested_key))
        if url:
            return url
    return ""


def _extract_product_review_metrics(*payloads: Any) -> tuple[float, int, int]:
    rating_value = _first_metric_value(
        _path_value(payloads, "product_info", "review_model", "product_overall_score"),
        _path_value(payloads, "product_info", "review_model", "overall_score"),
        _path_value(payloads, "product_info", "review_model", "rating_score"),
        _path_value(payloads, "product_info", "product_model", "rating_score"),
        _path_value(payloads, "rating_score"),
        _path_value(payloads, "review_info", "product_overall_score"),
        _path_value(payloads, "review_info", "review_ratings", "overall_score"),
        _find_first_nested_value(
            payloads,
            {
                "rating",
                "ratingscore",
                "reviewscore",
                "avgrating",
                "averagerating",
                "overallscore",
                "productoverallscore",
                "starrating",
                "productrating",
                "ratingstar",
                "reviewstar",
            },
        ),
    )
    review_count_value = _first_metric_value(
        _path_value(payloads, "product_info", "review_model", "product_review_count"),
        _path_value(payloads, "product_info", "review_model", "review_count"),
        _path_value(payloads, "review_info", "review_ratings", "review_count"),
        _path_value(payloads, "review_info", "total_reviews"),
        _find_first_nested_value(
            payloads,
            {
                "reviewcount",
                "reviewscount",
                "ratingcount",
                "ratingscount",
                "productreviewcount",
                "productreviews",
                "totalreviews",
            },
        ),
    )
    comment_count_value = _first_metric_value(
        _path_value(payloads, "review_info", "review_ratings", "review_count"),
        _path_value(payloads, "review_info", "total_reviews"),
        _find_first_nested_value(
            payloads,
            {
                "commentcount",
                "commentscount",
                "commentnum",
                "comments",
            },
        ),
    )
    review_count = _parse_int(review_count_value)
    comment_count = _parse_int(comment_count_value) or review_count
    return _parse_float(rating_value), review_count, comment_count


def _path_value(values: Any, *path: str) -> Any:
    roots = values if isinstance(values, (list, tuple)) else (values,)
    for root in roots:
        current = root
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current not in (None, "", [], {}):
            return current
    return None


def _first_metric_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _find_first_nested_value(values: Any, normalized_keys: set[str], *, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if isinstance(values, dict):
        for key, value in values.items():
            if _normalize_metric_key(key) in normalized_keys and value not in (None, "", [], {}):
                return value
        for value in values.values():
            found = _find_first_nested_value(value, normalized_keys, depth=depth + 1)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(values, (list, tuple)):
        for value in values:
            found = _find_first_nested_value(value, normalized_keys, depth=depth + 1)
            if found not in (None, "", [], {}):
                return found
    return None


def _normalize_metric_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _parse_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        normalized = value.replace(",", "").strip().lower()
        match = re.search(r"(\d+(?:\.\d+)?)\s*([km])?", normalized)
        if match:
            multiplier = {"k": 1_000, "m": 1_000_000}.get(match.group(2), 1)
            return int(float(match.group(1)) * multiplier)
    return 0


def _parse_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        normalized = value.replace(",", "").strip()
        match = re.search(r"(\d+(?:\.\d+)?)", normalized)
        if match:
            return float(match.group(1))
    return 0.0


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
    if "*" in text:
        return ""
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if match:
        return match.group(1)
    return ""


def _clean_shop_name(value: str) -> str:
    text = str(value or "").strip()
    return re.sub(r"^\s*(?:sold\s+by|seller|shop)\s*[:：]?\s*", "", text, flags=re.IGNORECASE).strip()


def _infer_currency_from_price_text(price_text: str) -> str:
    if "$" in price_text:
        return "USD"
    return ""


def _coerce_normalized_url(value: str) -> str:
    try:
        return normalize_tiktok_product_url(value)
    except ValueError:
        return ""


def _page_goto(page: Any, url: str, *, timeout_ms: int = 30000) -> None:
    navigate = getattr(page, "navigate", None)
    if callable(navigate):
        try:
            navigate(url, wait_until="domcontentloaded", timeout_ms=timeout_ms)
            return
        except TypeError:
            pass

    goto = getattr(page, "goto", None)
    if callable(goto):
        try:
            goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return
        except TypeError:
            try:
                goto(url, wait_until="domcontentloaded")
                return
            except TypeError:
                goto(url)
                return

    raise TypeError("Page object does not support navigation.")


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


def _random_delay_ms(min_ms: int, max_ms: int) -> int:
    normalized_min = max(int(min_ms), 0)
    normalized_max = max(int(max_ms), normalized_min)
    return random.randint(normalized_min, normalized_max)


def _wait_with_random_delay(page: Any, *, min_ms: int, max_ms: int) -> None:
    _safe_wait_for_timeout(page, _random_delay_ms(min_ms, max_ms))


def _wait_for_login_toast_to_settle(
    page: Any,
    *,
    settle_ms: int = DEFAULT_LOGIN_TOAST_SETTLE_MS,
    timeout_ms: int = DEFAULT_LOGIN_TOAST_TIMEOUT_MS,
    poll_ms: int = DEFAULT_LOGIN_TOAST_POLL_MS,
    stable_absent_polls: int = DEFAULT_LOGIN_TOAST_STABLE_POLLS,
) -> None:
    effective_poll_ms = max(int(poll_ms), 1)
    effective_settle_ms = max(int(settle_ms), effective_poll_ms)
    effective_timeout_ms = max(int(timeout_ms), effective_settle_ms)
    required_absent_polls = max(int(stable_absent_polls), 1)

    seen_toast = False
    absent_polls = 0
    elapsed_ms = 0
    latest_state: dict[str, Any] = {}

    while True:
        latest_state = _read_login_toast_state(page)
        if latest_state.get("visible"):
            seen_toast = True
            absent_polls = 0
        elif seen_toast:
            absent_polls += 1
            if absent_polls >= required_absent_polls:
                return
        elif elapsed_ms >= effective_settle_ms:
            return

        if elapsed_ms >= effective_timeout_ms:
            break

        _safe_wait_for_timeout(page, effective_poll_ms)
        elapsed_ms += effective_poll_ms

    if not seen_toast:
        return

    toast_text = str(latest_state.get("text", "")).strip()
    detail = f": {toast_text}" if toast_text else ""
    raise TikTokProductExtractionError(
        f"TikTok login toast did not disappear before timeout{detail}"
    )


def _read_login_toast_state(page: Any) -> dict[str, Any]:
    try:
        payload = page.evaluate(
            """(args) => {
                const selectors = args.toastSelectors || [];
                const keywords = (args.toastKeywords || [])
                  .map((item) => String(item || "").trim().toLowerCase())
                  .filter(Boolean);

                const normalizeText = (value) => String(value || "")
                  .replace(/\\s+/g, " ")
                  .trim();

                const isVisible = (element) => {
                  if (!element) return false;
                  const rect = element.getBoundingClientRect();
                  const style = window.getComputedStyle(element);
                  return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== "hidden" &&
                    style.display !== "none";
                };

                for (const selector of selectors) {
                  for (const element of document.querySelectorAll(selector)) {
                    if (!isVisible(element)) continue;

                    const text = normalizeText(element.textContent);
                    const loweredText = text.toLowerCase();
                    const className = normalizeText(
                      typeof element.className === "string" ? element.className : ""
                    ).toLowerCase();
                    const dataTestId = normalizeText(element.getAttribute("data-testid"));
                    const dataE2e = normalizeText(element.getAttribute("data-e2e"));
                    const role = normalizeText(element.getAttribute("role")).toLowerCase();
                    const signature = [selector, className, dataTestId, dataE2e, role]
                      .join(" ")
                      .toLowerCase();
                    const matchedKeyword = keywords.find((item) => loweredText.includes(item)) || "";
                    const loginLike = Boolean(matchedKeyword) ||
                      signature.includes("login") ||
                      signature.includes("sign-in") ||
                      signature.includes("signin");
                    const toastLike = signature.includes("toast") ||
                      role === "status" ||
                      role === "alert";

                    if (!loginLike || !toastLike) continue;

                    return {
                      visible: true,
                      text,
                      selector,
                      matched_keyword: matchedKeyword,
                    };
                  }
                }

                return {
                  visible: false,
                  text: "",
                  selector: "",
                  matched_keyword: "",
                };
            }""",
            {
                "toastSelectors": list(LOGIN_TOAST_CANDIDATE_SELECTORS),
                "toastKeywords": list(LOGIN_TOAST_KEYWORDS),
            },
        )
    except Exception:
        return {
            "visible": False,
            "text": "",
            "selector": "",
            "matched_keyword": "",
        }
    return payload if isinstance(payload, dict) else {}


def _safe_page_content(page: Any) -> str:
    content = getattr(page, "content", None)
    if callable(content):
        return str(content())
    return ""


def _safe_body_text(page: Any) -> str:
    try:
        locator = page.locator("body")
        inner_text = getattr(locator, "inner_text", None)
        if callable(inner_text):
            return str(inner_text(timeout=3000) or "").strip()
    except Exception:
        return ""
    return ""


def _wait_for_security_check_intervention(
    page: Any,
    *,
    product_url: str,
    timeout_ms: int = DEFAULT_SECURITY_CHECK_GRACE_MS,
    poll_ms: int = DEFAULT_SECURITY_CHECK_POLL_MS,
) -> tuple[str, str, dict[str, Any], str | None]:
    effective_timeout_ms = max(int(timeout_ms), 0)
    effective_poll_ms = max(int(poll_ms), 1)
    deadline = time.monotonic() + effective_timeout_ms / 1000.0

    latest_html = ""
    latest_resolved_url = str(getattr(page, "url", "") or product_url)
    latest_dom_snapshot: dict[str, Any] = {}
    latest_security_message: str | None = None

    while True:
        latest_dom_snapshot = _read_dom_product_snapshot(page)
        latest_html = _safe_page_content(page)
        latest_resolved_url = str(getattr(page, "url", "") or product_url)
        latest_security_message = _detect_browser_security_check(
            page,
            html=latest_html,
            resolved_url=latest_resolved_url,
            dom_snapshot=latest_dom_snapshot,
        )
        if not latest_security_message:
            if int(latest_dom_snapshot.get("visible_signal_count", 0)) < 2:
                remaining_ms = max(int((deadline - time.monotonic()) * 1000), 0)
                if remaining_ms > 0:
                    latest_dom_snapshot = _wait_for_product_page_ready(
                        page,
                        timeout_ms=remaining_ms,
                        source_url=product_url,
                    )
                    latest_html = _safe_page_content(page)
                    latest_resolved_url = str(getattr(page, "url", "") or product_url)
                    latest_security_message = _detect_browser_security_check(
                        page,
                        html=latest_html,
                        resolved_url=latest_resolved_url,
                        dom_snapshot=latest_dom_snapshot,
                    )
            return latest_html, latest_resolved_url, latest_dom_snapshot, latest_security_message

        if time.monotonic() >= deadline:
            return latest_html, latest_resolved_url, latest_dom_snapshot, latest_security_message

        _safe_wait_for_timeout(page, effective_poll_ms)


def _detect_browser_security_check(
    page: Any,
    *,
    html: str,
    resolved_url: str,
    dom_snapshot: dict[str, Any],
) -> str | None:
    visible_signal_count = int(dom_snapshot.get("visible_signal_count", 0))
    has_router_data = bool(html and "__MODERN_ROUTER_DATA__" in html)
    if visible_signal_count >= 2 and has_router_data:
        return None

    normalized_url = _normalize_browser_text(resolved_url)
    normalized_body = _normalize_browser_text(_safe_body_text(page))
    normalized_html = _normalize_browser_text(html)

    signal = _find_security_check_signal(
        (normalized_url, normalized_body),
        SECURITY_CHECK_STRONG_SIGNALS + SECURITY_CHECK_WEAK_SIGNALS,
    )
    if signal:
        return f"TikTok security check detected: {signal}"

    signal = _find_security_check_signal((normalized_html,), SECURITY_CHECK_STRONG_SIGNALS)
    if signal:
        return f"TikTok security check detected: {signal}"

    if visible_signal_count > 0 or has_router_data:
        return None

    signal = _find_security_check_signal((normalized_html,), SECURITY_CHECK_HTML_FALLBACK_SIGNALS)
    if signal:
        return f"TikTok security check detected: {signal}"
    return None


def _find_security_check_signal(haystacks: tuple[str, ...], signals: tuple[str, ...]) -> str | None:
    for signal in signals:
        for haystack in haystacks:
            if haystack and signal in haystack:
                return signal
    return None


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
    request_pacer: RequestPacer | None = None,
    pacer_key: str = "tiktok:http",
) -> Any:
    if request_pacer is not None:
        request_pacer.wait_before_request(pacer_key)
    if session is not None:
        try:
            response = session.get(
                url,
                headers=headers,
                timeout=timeout,
                allow_redirects=allow_redirects,
            )
            _raise_tiktok_http_status(response)
            return response
        finally:
            if request_pacer is not None:
                request_pacer.mark_request_finished(pacer_key)

    if requests is not None:
        try:
            with requests.Session() as active_session:
                response = active_session.get(
                    url,
                    headers=headers,
                    timeout=timeout,
                    allow_redirects=allow_redirects,
                )
                _raise_tiktok_http_status(response)
                return response
        finally:
            if request_pacer is not None:
                request_pacer.mark_request_finished(pacer_key)

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
        _raise_tiktok_http_status(response)
        return response
    except URLError as exc:
        raise TikTokProductExtractionError(str(exc.reason)) from exc
    finally:
        if request_pacer is not None:
            request_pacer.mark_request_finished(pacer_key)


def _raise_tiktok_http_status(response: Any) -> None:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code in {403, 408, 409, 418, 425, 429}:
        raise TikTokRateLimitError(f"HTTP {status_code}")
    if status_code >= 400:
        raise TikTokProductExtractionError(f"HTTP {status_code}")
    response.raise_for_status()
