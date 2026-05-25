from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from automation_business_scaffold.capabilities.browser.page_primitives import (
    click_first_visible_locator,
    page_goto,
    safe_wait_for_timeout,
)
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    coerce_mapping,
    coerce_mapping_list,
    coerce_str,
    failed_result,
    first_non_empty,
    success_result,
)
from automation_business_scaffold.domains.tiktok.flows.outreach_product_videos import (
    match_outreach_rows_to_videos,
    normalize_product_video_rows,
    persist_product_video_audit,
)
from automation_business_scaffold.infrastructure.browser.browser_bridge import open_automation_page


PRODUCT_VIDEO_TAB_SELECTORS = (
    'text="商品关联视频"',
    'text="关联视频"',
    'text="带货视频"',
    'text="Videos"',
)
NEXT_PAGE_SELECTORS = (
    'button:has-text("下一页")',
    'button:has-text("Next")',
    '.ant-pagination-next:not(.ant-pagination-disabled)',
)


def fastmoss_product_video_outreach_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    product_id = first_non_empty(payload.get("product_id"), coerce_mapping(payload.get("product_identity")).get("product_id"))
    rows = coerce_mapping_list(payload.get("rows"))
    query_window = coerce_mapping(payload.get("query_window")) or {"mode": "d_type", "d_type": 90}
    trigger_date = first_non_empty(payload.get("trigger_date"), date.today().isoformat())

    try:
        videos = collect_product_videos_with_browser(context=context, payload=payload, product_id=product_id, query_window=query_window)
    except Exception as exc:
        return failed_result(
            context,
            error=build_error(
                error_type="browser_failure",
                error_code="fastmoss_product_video_browser_collect_failed",
                message=str(exc),
                retryable=True,
                details={"product_id": product_id},
            ),
            summary={"product_id": product_id, "fetch_status": "failed", "collection_path": "browser"},
            result={"product_id": product_id, "fetch_status": "failed", "collection_path": "browser", "error": str(exc)},
        )

    result = match_outreach_rows_to_videos(
        product_id=product_id,
        rows=rows,
        videos=videos,
        query_window=query_window,
        trigger_date=trigger_date,
    )
    audit = persist_product_video_audit(context=context, product_id=product_id, videos=videos)
    result["collection_path"] = "browser"
    result["query_window_applied"] = True
    result["video_audit"] = audit
    result["summary"].update(
        {
            "collection_path": "browser",
            "video_audit_ref": audit.get("json_path", ""),
            "unique_creator_count": audit.get("unique_creator_count", 0),
            "query_window_applied": True,
        }
    )
    return success_result(context, summary=result["summary"], result=result)


def collect_product_videos_with_browser(
    *,
    context: HandlerContext,
    payload: Mapping[str, Any],
    product_id: str,
    query_window: Mapping[str, Any],
) -> list[dict[str, Any]]:
    mock_pages = coerce_mapping_list(payload.get("mock_browser_video_pages"))
    if mock_pages:
        rows: list[dict[str, Any]] = []
        for page_number, page in enumerate(mock_pages, start=1):
            for row_index, row in enumerate(coerce_mapping_list(page.get("rows")), start=1):
                item = dict(row)
                item.setdefault("product_id", product_id)
                item.setdefault("page_number", page_number)
                item.setdefault("row_index", row_index)
                rows.append(item)
        return normalize_product_video_rows(rows)

    if not product_id:
        return []
    return normalize_product_video_rows(
        _collect_product_videos_from_browser_page(
            context=context,
            payload=payload,
            product_id=product_id,
            query_window=query_window,
        )
    )


def _collect_product_videos_from_browser_page(
    *,
    context: HandlerContext,
    payload: Mapping[str, Any],
    product_id: str,
    query_window: Mapping[str, Any],
) -> list[dict[str, Any]]:
    profile_ref = first_non_empty(
        payload.get("fastmoss_browser_profile_ref"),
        payload.get("browser_profile_ref"),
        payload.get("profile_ref"),
    )
    provider_name = first_non_empty(payload.get("fastmoss_browser_provider_name"), payload.get("browser_provider_name"))
    workspace_id = _optional_int(first_non_empty(payload.get("fastmoss_browser_workspace_id"), payload.get("browser_workspace_id")))
    profile_id = first_non_empty(payload.get("fastmoss_browser_profile_id"), payload.get("browser_profile_id")) or None
    product_url = first_non_empty(
        payload.get("fastmoss_product_video_url"),
        payload.get("fastmoss_product_url"),
        f"https://www.fastmoss.com/zh/e-commerce/detail/{product_id}",
    )
    max_pages = _positive_int(payload.get("browser_video_max_pages"), default=200)
    wait_ms = _positive_int(payload.get("browser_video_page_wait_ms"), default=1200)
    tab_selectors = tuple(coerce_str(item) for item in payload.get("browser_video_tab_selectors", []) if coerce_str(item)) or PRODUCT_VIDEO_TAB_SELECTORS
    next_selectors = tuple(coerce_str(item) for item in payload.get("browser_video_next_selectors", []) if coerce_str(item)) or NEXT_PAGE_SELECTORS

    rows: list[dict[str, Any]] = []
    seen_signatures: set[str] = set()
    with open_automation_page(
        profile_ref=profile_ref or None,
        workspace_id=workspace_id,
        profile_id=profile_id,
        provider_name=provider_name or None,
        headless=False,
        force_open=bool(payload.get("browser_force_open")),
    ) as session:
        page = session.raw_page
        page_goto(page, product_url, timeout_ms=_positive_int(payload.get("browser_goto_timeout_ms"), default=60_000))
        safe_wait_for_timeout(page, wait_ms)
        click_first_visible_locator(page, tab_selectors)
        safe_wait_for_timeout(page, wait_ms)
        for page_number in range(1, max_pages + 1):
            page_rows = _extract_video_rows_from_page(page, product_id=product_id, page_number=page_number)
            signature = _page_signature(page_rows)
            if signature and signature in seen_signatures:
                break
            if signature:
                seen_signatures.add(signature)
            rows.extend(page_rows)
            if not click_first_visible_locator(page, next_selectors):
                break
            safe_wait_for_timeout(page, wait_ms)
    return rows


def _extract_video_rows_from_page(page: Any, *, product_id: str, page_number: int) -> list[dict[str, Any]]:
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return []
    try:
        payload = evaluate(
            r"""
            () => {
                const links = Array.from(document.querySelectorAll('a[href*="/video/"], a[href*="tiktok.com/@"]'));
                const rows = [];
                const seen = new Set();
                for (const link of links) {
                    const href = link.href || link.getAttribute('href') || '';
                    const videoMatch = href.match(/\/video\/(\d+)/);
                    const creatorMatch = href.match(/@([^/?#]+)/);
                    if (!videoMatch || !creatorMatch) {
                        continue;
                    }
                    const key = creatorMatch[1] + ':' + videoMatch[1];
                    if (seen.has(key)) {
                        continue;
                    }
                    seen.add(key);
                    const container = link.closest('tr,[role="row"],.ant-table-row,.video-card,.card,li,div') || link;
                    rows.push({
                        unique_id: decodeURIComponent(creatorMatch[1]),
                        video_id: videoMatch[1],
                        video_url: href,
                        raw_text: (container.innerText || link.innerText || '').trim(),
                        source_selector: 'a[href*="/video/"]'
                    });
                }
                return rows;
            }
            """
        )
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for row_index, row in enumerate(payload if isinstance(payload, list) else [], start=1):
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["product_id"] = product_id
        item["page_number"] = page_number
        item["row_index"] = row_index
        rows.append(item)
    return rows


def _page_signature(rows: list[Mapping[str, Any]]) -> str:
    return "|".join(f"{row.get('unique_id') or row.get('creator_unique_id')}:{row.get('video_id')}" for row in rows)


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(float(coerce_str(value)))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _optional_int(value: Any) -> int | None:
    try:
        return int(float(coerce_str(value)))
    except (TypeError, ValueError):
        return None


__all__ = ["collect_product_videos_with_browser", "fastmoss_product_video_outreach_handler"]
