from __future__ import annotations

import os
import re
import time
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from automation_business_scaffold.config import get_execution_control_defaults
from automation_business_scaffold.extend_script.feishu_api import FeishuBitableClient, parse_table_url
from automation_business_scaffold.flows.browser_bridge import open_automation_page
from automation_business_scaffold.flows.execution_control_flow import build_controlled_resource_code
from automation_business_scaffold.flows.fastmoss_http_session import (
    FastMossHTTPError,
    FastMossHTTPSession,
)
from automation_business_scaffold.flows.fastmoss_product_flow import (
    _ensure_fastmoss_logged_in,
    _fastmoss_blocked_handling,
    _fastmoss_blocker_rules,
    _is_fastmoss_account_logged_in,
)
from automation_business_scaffold.flows.influencer_pool_support import (
    DEFAULT_COMPETITOR_HOLIDAY_FIELD_NAME,
    DEFAULT_COMPETITOR_IMAGE_FIELD_NAME,
    DEFAULT_COMPETITOR_PRODUCT_ID_FIELD_NAME,
    DEFAULT_COMPETITOR_STATUS_FIELD_NAME,
    DEFAULT_COMPETITOR_SYNC_STATUS_FIELD_NAME,
    DEFAULT_INFLUENCER_ID_FIELD_NAME,
    InfluencerTraceError,
    build_influencer_record_index,
    build_influencer_write_fields,
    format_influencer_contacts,
    influencer_state_has_source_product,
    load_table_schema,
    merge_influencer_facts,
    persist_influencer_entity_snapshot,
)
from automation_business_scaffold.flows.phase1_runtime_store import Phase1RuntimeStore

RUN_MODES_WITH_MUTATIONS = {"canary", "full_auto"}
UNAVAILABLE_PRODUCT_STATUS_VALUE = "已下架/区域不可售"
SOURCE_STATUS_PENDING_VALUES = {"", "待查找", "失败重试", "处理中"}
INFLUENCER_POOL_SINGLE_ROW_TASK_NAME = "sync_tk_influencer_pool_single_row"
INFLUENCER_POOL_AUTHOR_DETAIL_TASK_NAME = "sync_tk_influencer_pool_author_detail"
FASTMOSS_HARD_STOP_RESPONSE_CODE = "MAG_AUTH_3002"
AUTHOR_LIST_FOLLOWER_COUNT_KEYS = (
    "follower_count",
    "fans_count",
    "follower_cnt",
    "fans_cnt",
    "follower_num",
    "fans_num",
    "followers",
    "fans",
    "follower_count_show",
    "fans_count_show",
)
DEFAULT_FASTMOSS_BROWSER_SYNC_STEP_DELAY_SECONDS = 0.5
DEFAULT_FASTMOSS_BROWSER_SYNC_LOGIN_SETTLE_SECONDS = 2.0
DEFAULT_FASTMOSS_BROWSER_RISK_WAIT_TIMEOUT_SECONDS = 90.0
DEFAULT_FASTMOSS_BROWSER_RISK_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_DEBUG_TIMELINE_MAX_EVENTS = 1500
DEFAULT_MAX_AUTHOR_DETAIL_JOBS_PER_SOURCE_ROW = 50
DEFAULT_INFLUENCER_POOL_WORKER_MAX_ITERATIONS = 1
SOURCE_STATUS_AWAITING_CAPTCHA_VALUE = "风控待处理"


@dataclass(slots=True)
class BrowserCaptchaSignals:
    captcha_config_seen: bool = False
    captcha_verify_ok: bool = False
    core_success_paths: tuple[str, ...] = ()

    def add_core_success(self, path: str) -> None:
        normalized = str(path or "").strip()
        existing = set(self.core_success_paths)
        if normalized and normalized not in existing:
            self.core_success_paths = tuple((*self.core_success_paths, normalized))

    def is_recovered(self) -> bool:
        required = {"/api/goods/v3/base", "/api/goods/v3/author"}
        return required.issubset(set(self.core_success_paths))


@dataclass(slots=True)
class TableTarget:
    client: FeishuBitableClient
    table_url: str
    app_token: str
    table_id: str
    view_id: str


@dataclass(slots=True)
class SyntheticExecutionContext:
    request_id: str
    execution_id: str
    run_id: str


class _BrowserExecutionHeartbeat:
    def __init__(
        self,
        *,
        store: Phase1RuntimeStore,
        execution_id: str,
        lease_seconds: float,
        interval_seconds: float,
    ):
        self._store = store
        self._execution_id = execution_id
        self._lease_seconds = lease_seconds
        self._interval_seconds = max(min(interval_seconds, lease_seconds), 0.1)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                self._store.heartbeat_browser_execution(
                    execution_id=self._execution_id,
                    lease_seconds=self._lease_seconds,
                )
            except Exception:
                return

    def __enter__(self) -> "_BrowserExecutionHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop_event.set()
        self._thread.join(timeout=self._interval_seconds + 1.0)


def run_sync_tk_influencer_pool(params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_sync_settings(params)
    debug_timeline, debug_event_callback = _create_debug_timeline_collector(settings)
    settings["debug_event_callback"] = debug_event_callback
    queue_settings = _build_queue_settings(params)
    queue_mode = str(settings.get("queue_mode", "inline") or "inline")
    if queue_mode in {"dispatch", "dispatch_only", "dispatch-only"}:
        return _dispatch_influencer_pool_product_jobs(
            params=params,
            settings=settings,
            queue_settings=queue_settings,
            debug_timeline=debug_timeline,
        )
    if queue_mode in {"worker", "worker_drain", "worker-drain", "daemon"}:
        return _run_influencer_pool_worker_daemon(
            params=params,
            settings=settings,
            queue_settings=queue_settings,
            debug_timeline=debug_timeline,
        )
    source_target = _build_table_target(settings["table_url"], settings["access_token"])
    target_target = _build_table_target(settings["target_table_url"], settings["access_token"])
    target_schema = load_table_schema(target_target.client, settings["target_table_url"])
    store = _create_runtime_store(params)
    execution = SyntheticExecutionContext(
        request_id=str(params.get("request_id", "") or ""),
        execution_id=str(params.get("execution_id", "") or uuid.uuid4().hex),
        run_id=str(params.get("run_id", "") or f"sync-tk-influencer-pool-{int(time.time())}"),
    )

    source_records = source_target.client.list_all_records(
        app_token=source_target.app_token,
        table_id=source_target.table_id,
        page_size=100,
        view_id=source_target.view_id or None,
    )
    target_records = target_target.client.list_all_records(
        app_token=target_target.app_token,
        table_id=target_target.table_id,
        page_size=100,
        view_id=target_target.view_id or None,
    )

    snapshot_records = _load_influencer_snapshot_records(store, target_records)
    influencer_index = build_influencer_record_index(
        target_records,
        snapshots=snapshot_records,
        influencer_id_field_name=DEFAULT_INFLUENCER_ID_FIELD_NAME,
    )

    candidate_rows = [
        raw_record
        for raw_record in source_records
        if _is_candidate_source_record(raw_record)
    ]
    max_source_rows = int(settings["max_source_rows"])
    if max_source_rows > 0:
        candidate_rows = candidate_rows[:max_source_rows]

    items, failed_items = _run_candidate_rows_via_worker_queue(
        params=params,
        settings=settings,
        queue_settings=queue_settings,
        candidate_rows=candidate_rows,
        source_target=source_target,
        target_target=target_target,
        target_schema=target_schema,
        influencer_index=influencer_index,
        store=store,
        execution=execution,
    )

    summary = _summarize_status_counts(items)
    write_summary = _build_write_summary(items, failed_items)
    outbox: list[dict[str, Any]] = []
    outbox_error = ""
    try:
        outbox = _create_influencer_pool_summary_outbox(
            params=params,
            store=store,
            execution=execution,
            summary=summary,
            write_summary=write_summary,
            items=items,
            failed_items=failed_items,
        )
    except Exception as exc:
        outbox_error = str(exc)
    return {
        "status": "success",
        "message": f"Processed {summary['total']} competitor rows for influencer pool sync.",
        "summary": summary,
        "write_summary": write_summary,
        "item": items[0] if items else {},
        "items": items,
        "failed_items": failed_items,
        "failed_item_count": len(failed_items),
        "outbox": outbox,
        "outbox_error": outbox_error,
        "settings": {
            "run_mode": settings["run_mode"],
            "table_url": settings["table_url"],
            "target_table_url": settings["target_table_url"],
            "include_contact": bool(settings["include_contact"]),
            "debug_cookie_timeline": bool(settings["debug_cookie_timeline"]),
            "request_delay_range_seconds": [
                float(settings["request_delay_min_seconds"]),
                float(settings["request_delay_max_seconds"]),
            ],
            "worker_queue_resource_code": queue_settings["resource_code"],
            "worker_id": queue_settings["worker_id"],
        },
        "debug_timeline_summary": {
            "enabled": bool(settings["debug_cookie_timeline"]),
            "event_count": len(debug_timeline),
            "max_events": int(settings["debug_timeline_max_events"]),
        },
        "debug_timeline": debug_timeline,
    }


def _create_debug_timeline_collector(
    settings: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], Callable[[dict[str, Any]], None]]:
    if not bool(settings.get("debug_cookie_timeline", False)):
        return [], lambda event: None

    timeline: list[dict[str, Any]] = []
    max_events = max(int(settings.get("debug_timeline_max_events") or DEFAULT_DEBUG_TIMELINE_MAX_EVENTS), 1)
    lock = threading.Lock()
    dropped_count = 0

    def _record(event: dict[str, Any]) -> None:
        nonlocal dropped_count
        if not isinstance(event, Mapping):
            return
        payload = {"ts_ms": int(time.time() * 1000)}
        payload.update({str(key): value for key, value in event.items() if value is not None})
        with lock:
            if len(timeline) < max_events:
                timeline.append(payload)
                return
            dropped_count += 1
            if timeline and timeline[-1].get("kind") == "debug_timeline_truncated":
                timeline[-1]["dropped_events"] = dropped_count
                timeline[-1]["ts_ms"] = payload["ts_ms"]
                return
            timeline.append(
                {
                    "ts_ms": payload["ts_ms"],
                    "kind": "debug_timeline_truncated",
                    "dropped_events": dropped_count,
                }
            )

    return timeline, _record


def _emit_debug_event(settings: Mapping[str, Any], kind: str, **payload: Any) -> None:
    callback = settings.get("debug_event_callback")
    if not callable(callback):
        return
    event = {"kind": str(kind), "ts_ms": int(time.time() * 1000)}
    event.update({str(key): value for key, value in payload.items() if value is not None})
    try:
        callback(event)
    except Exception:
        return


def _dispatch_influencer_pool_product_jobs(
    *,
    params: Mapping[str, Any],
    settings: Mapping[str, Any],
    queue_settings: Mapping[str, Any],
    debug_timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    source_target = _build_table_target(settings["table_url"], settings["access_token"])
    store = _create_runtime_store(params)
    source_records = source_target.client.list_all_records(
        app_token=source_target.app_token,
        table_id=source_target.table_id,
        page_size=100,
        view_id=source_target.view_id or None,
    )
    candidate_rows = [raw_record for raw_record in source_records if _is_candidate_source_record(raw_record)]
    max_source_rows = int(settings["max_source_rows"])
    if max_source_rows > 0:
        candidate_rows = candidate_rows[:max_source_rows]

    jobs = [_build_product_job_from_source_record(raw_record) for raw_record in candidate_rows]
    queue_payload = _upsert_product_jobs(
        store=store,
        jobs=jobs,
        force_refresh=_coerce_bool(params.get("force_refresh_product_jobs"), default=False),
    )
    _emit_debug_event(
        settings,
        "influencer_pool_product_jobs_dispatched",
        candidate_count=len(candidate_rows),
        created_count=queue_payload.get("created_count", 0),
        updated_count=queue_payload.get("updated_count", 0),
        kept_terminal_count=queue_payload.get("kept_terminal_count", 0),
    )
    summary = {
        "total": len(candidate_rows),
        "counts": {
            "product_jobs_created": int(queue_payload.get("created_count") or 0),
            "product_jobs_updated": int(queue_payload.get("updated_count") or 0),
            "product_jobs_kept_terminal": int(queue_payload.get("kept_terminal_count") or 0),
        },
    }
    return {
        "status": "success",
        "message": f"Dispatched {len(candidate_rows)} influencer-pool product jobs.",
        "summary": summary,
        "queue_mode": "dispatch_only",
        "product_queue": queue_payload,
        "settings": {
            "run_mode": settings["run_mode"],
            "table_url": settings["table_url"],
            "target_table_url": settings["target_table_url"],
            "worker_queue_resource_code": queue_settings["resource_code"],
            "worker_id": queue_settings["worker_id"],
        },
        "debug_timeline_summary": {
            "enabled": bool(settings["debug_cookie_timeline"]),
            "event_count": len(debug_timeline),
            "max_events": int(settings["debug_timeline_max_events"]),
        },
        "debug_timeline": debug_timeline,
    }


def _run_influencer_pool_worker_daemon(
    *,
    params: Mapping[str, Any],
    settings: Mapping[str, Any],
    queue_settings: Mapping[str, Any],
    debug_timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    worker_settings = _build_influencer_pool_worker_settings(params)
    source_target = _build_table_target(settings["table_url"], settings["access_token"])
    target_target = _build_table_target(settings["target_table_url"], settings["access_token"])
    target_schema = load_table_schema(target_target.client, settings["target_table_url"])
    store = _create_runtime_store(params)
    execution = SyntheticExecutionContext(
        request_id=str(params.get("request_id", "") or ""),
        execution_id=str(params.get("execution_id", "") or uuid.uuid4().hex),
        run_id=str(params.get("run_id", "") or f"sync-tk-influencer-pool-worker-{int(time.time())}"),
    )

    target_records = target_target.client.list_all_records(
        app_token=target_target.app_token,
        table_id=target_target.table_id,
        page_size=100,
        view_id=target_target.view_id or None,
    )
    snapshot_records = _load_influencer_snapshot_records(store, target_records)
    influencer_index = build_influencer_record_index(
        target_records,
        snapshots=snapshot_records,
        influencer_id_field_name=DEFAULT_INFLUENCER_ID_FIELD_NAME,
    )

    items: list[dict[str, Any]] = []
    failed_items: list[dict[str, Any]] = []
    worker_counts = {
        "product_jobs_processed": 0,
        "author_jobs_processed": 0,
        "finalizer_jobs_processed": 0,
        "idle_iterations": 0,
    }
    hard_stopped = False
    worker_kinds = set(worker_settings["worker_kinds"])
    max_iterations = int(worker_settings["max_iterations"])
    idle_cycles = 0
    iteration = 0

    with FastMossHTTPSession(
        phone=str(settings["fastmoss_phone"]),
        password=str(settings["fastmoss_password"]),
        default_region=str(settings["fastmoss_region"]),
        request_delay_range=(
            float(settings["request_delay_min_seconds"]),
            float(settings["request_delay_max_seconds"]),
        ),
        event_callback=settings.get("debug_event_callback"),
    ) as fastmoss:
        _prime_fastmoss_session(
            fastmoss=fastmoss,
            settings=settings,
        )
        while max_iterations <= 0 or iteration < max_iterations:
            iteration += 1
            iteration_items: list[dict[str, Any]] = []

            if "product" in worker_kinds and not hard_stopped:
                product_item = _run_one_influencer_pool_product_worker(
                    settings=settings,
                    queue_settings=queue_settings,
                    source_target=source_target,
                    target_target=target_target,
                    target_schema=target_schema,
                    influencer_index=influencer_index,
                    fastmoss=fastmoss,
                    store=store,
                    execution=execution,
                )
                if product_item:
                    iteration_items.append(product_item)
                    worker_counts["product_jobs_processed"] += 1

            if "author" in worker_kinds and not hard_stopped:
                author_item = _run_one_influencer_pool_author_worker(
                    settings=settings,
                    queue_settings=queue_settings,
                    source_target=source_target,
                    target_target=target_target,
                    target_schema=target_schema,
                    influencer_index=influencer_index,
                    fastmoss=fastmoss,
                    store=store,
                    execution=execution,
                )
                if author_item:
                    iteration_items.append(author_item)
                    worker_counts["author_jobs_processed"] += 1

            if "finalizer" in worker_kinds and not hard_stopped:
                finalizer_item = _run_one_influencer_pool_finalizer(
                    settings=settings,
                    source_target=source_target,
                    store=store,
                    execution=execution,
                    limit=int(worker_settings["finalizer_scan_limit"]),
                )
                if finalizer_item:
                    iteration_items.append(finalizer_item)
                    worker_counts["finalizer_jobs_processed"] += 1

            for item in iteration_items:
                items.append(item)
                if item.get("status") in {"failed", "failed_retry"}:
                    failed_items.append(item)
                if _item_requests_hard_stop(item):
                    hard_stopped = True

            if iteration_items:
                idle_cycles = 0
            else:
                idle_cycles += 1
                worker_counts["idle_iterations"] += 1

            if hard_stopped:
                break
            if bool(worker_settings["stop_when_idle"]) and idle_cycles >= int(worker_settings["max_idle_cycles"]):
                break
            if not iteration_items and float(worker_settings["poll_interval_seconds"]) > 0:
                time.sleep(float(worker_settings["poll_interval_seconds"]))

    summary = _summarize_status_counts(items)
    write_summary = _build_write_summary(items, failed_items)
    return {
        "status": "success",
        "message": f"Processed {len(items)} influencer-pool queue worker items.",
        "summary": summary,
        "write_summary": write_summary,
        "queue_mode": "worker",
        "worker": {
            **worker_counts,
            "worker_kinds": sorted(worker_kinds),
            "iterations": iteration,
            "hard_stopped": hard_stopped,
        },
        "items": items,
        "failed_items": failed_items,
        "failed_item_count": len(failed_items),
        "settings": {
            "run_mode": settings["run_mode"],
            "table_url": settings["table_url"],
            "target_table_url": settings["target_table_url"],
            "include_contact": bool(settings["include_contact"]),
            "worker_queue_resource_code": queue_settings["resource_code"],
            "worker_id": queue_settings["worker_id"],
        },
        "debug_timeline_summary": {
            "enabled": bool(settings["debug_cookie_timeline"]),
            "event_count": len(debug_timeline),
            "max_events": int(settings["debug_timeline_max_events"]),
        },
        "debug_timeline": debug_timeline,
    }


def _run_candidate_rows_via_worker_queue(
    *,
    params: Mapping[str, Any],
    settings: Mapping[str, Any],
    queue_settings: Mapping[str, Any],
    candidate_rows: list[dict[str, Any]],
    source_target: TableTarget,
    target_target: TableTarget,
    target_schema: Mapping[str, Any],
    influencer_index: dict[str, dict[str, Any]],
    store: Phase1RuntimeStore,
    execution: SyntheticExecutionContext,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    failed_items: list[dict[str, Any]] = []

    with FastMossHTTPSession(
        phone=str(settings["fastmoss_phone"]),
        password=str(settings["fastmoss_password"]),
        default_region=str(settings["fastmoss_region"]),
        request_delay_range=(
            float(settings["request_delay_min_seconds"]),
            float(settings["request_delay_max_seconds"]),
        ),
        event_callback=settings.get("debug_event_callback"),
    ) as fastmoss:
        _prime_fastmoss_session(
            fastmoss=fastmoss,
            settings=settings,
        )
        for raw_record in candidate_rows:
            item = _run_single_row_worker_via_queue(
                params=params,
                raw_record=raw_record,
                settings=settings,
                queue_settings=queue_settings,
                source_target=source_target,
                target_target=target_target,
                target_schema=target_schema,
                influencer_index=influencer_index,
                fastmoss=fastmoss,
                store=store,
                execution=execution,
            )
            items.append(item)
            if item["status"] in {"failed", "failed_retry"}:
                failed_items.append(item)
            if _item_requests_hard_stop(item):
                _emit_debug_event(
                    settings,
                    "influencer_pool_hard_stop",
                    record_id=str(item.get("record_id", "") or ""),
                    product_id=str(item.get("product_id", "") or ""),
                    hard_stop_code=str(item.get("hard_stop_code", "") or ""),
                    hard_stop_reason=str(item.get("hard_stop_reason", "") or ""),
                )
                break
    return items, failed_items


def _prime_fastmoss_session(
    *,
    fastmoss: FastMossHTTPSession,
    settings: Mapping[str, Any],
) -> None:
    _emit_debug_event(
        settings,
        "session_prime_start",
        has_browser_cookie_source=False,
        **fastmoss.cookie_snapshot(),
    )
    _emit_debug_event(settings, "session_prime_http_login_fallback_start", **fastmoss.cookie_snapshot())
    fastmoss.ensure_logged_in()
    _emit_debug_event(settings, "session_prime_http_login_fallback_done", **fastmoss.cookie_snapshot())


def _refresh_fastmoss_session_from_browser(
    *,
    fastmoss: FastMossHTTPSession,
    settings: Mapping[str, Any],
    reason: str,
    product_id: str = "",
    author_uid: str = "",
    retry_probe: Callable[[], dict[str, Any]] | None = None,
    initial_error: FastMossHTTPError | None = None,
) -> Any:
    browser_session_kwargs = _build_browser_session_kwargs(settings)
    if browser_session_kwargs is None:
        if initial_error is not None:
            raise initial_error
        raise ValueError("Browser target is not configured for FastMoss cookie sync")

    retry_wait_timeout_seconds = float(settings["browser_risk_wait_timeout_seconds"])
    retry_poll_interval_seconds = float(settings["browser_risk_poll_interval_seconds"])
    browser_url = _build_browser_recovery_url(
        product_id=product_id,
        author_uid=author_uid,
    )
    last_error = initial_error
    _emit_debug_event(
        settings,
        "browser_session_sync_start",
        reason=reason,
        product_id=product_id,
        author_uid=author_uid,
        browser_url=browser_url,
        has_initial_error=initial_error is not None,
        **fastmoss.cookie_snapshot(),
    )

    with open_automation_page(
        **browser_session_kwargs,
        force_open=False,
        blocked_handling=_fastmoss_blocked_handling(),
        blocker_rules=_fastmoss_blocker_rules(),
    ) as browser_session:
        page = browser_session.raw_page
        signals = BrowserCaptchaSignals()
        response_listener = _build_browser_captcha_response_listener(
            signals,
            event_callback=lambda kind, **payload: _emit_debug_event(settings, kind, **payload),
        )
        page.on("response", response_listener)
        page.goto("https://www.fastmoss.com/zh/account/center", wait_until="load")
        browser_cookies = page.context.cookies(["https://www.fastmoss.com"])
        browser_logged_in = _is_fastmoss_account_logged_in(page)
        _emit_debug_event(
            settings,
            "browser_account_check",
            reason=reason,
            browser_logged_in=browser_logged_in,
            browser_cookie_count=len(browser_cookies),
            browser_has_fd_tk=_browser_cookie_list_has_fd_tk(browser_cookies),
        )
        if not browser_logged_in or not _browser_cookie_list_has_fd_tk(browser_cookies):
            _emit_debug_event(
                settings,
                "browser_login_required",
                reason=reason,
                browser_logged_in=browser_logged_in,
                browser_has_fd_tk=_browser_cookie_list_has_fd_tk(browser_cookies),
            )
            _ensure_fastmoss_logged_in(
                page,
                phone=str(settings["fastmoss_phone"]),
                password=str(settings["fastmoss_password"]),
                step_delay_sec=DEFAULT_FASTMOSS_BROWSER_SYNC_STEP_DELAY_SECONDS,
                login_settle_sec=DEFAULT_FASTMOSS_BROWSER_SYNC_LOGIN_SETTLE_SECONDS,
            )
            page.goto("https://www.fastmoss.com/zh/account/center", wait_until="load")
            browser_cookies = page.context.cookies(["https://www.fastmoss.com"])
            browser_logged_in = _is_fastmoss_account_logged_in(page)
            _emit_debug_event(
                settings,
                "browser_login_check_after_reauth",
                reason=reason,
                browser_logged_in=browser_logged_in,
                browser_cookie_count=len(browser_cookies),
                browser_has_fd_tk=_browser_cookie_list_has_fd_tk(browser_cookies),
            )
            if not browser_logged_in or not _browser_cookie_list_has_fd_tk(browser_cookies):
                raise FastMossHTTPError(
                    "Roxy browser session is still guest after FastMoss re-login",
                    stage="browser.session_sync",
                    method="GET",
                    path="/zh/account/center",
                )
        if browser_url:
            page.goto(browser_url, wait_until="load")
            _emit_debug_event(
                settings,
                "browser_recovery_page_opened",
                reason=reason,
                browser_url=browser_url,
            )

        browser_cookies = page.context.cookies(["https://www.fastmoss.com"])
        synced_cookie_count = fastmoss.replace_browser_cookies(browser_cookies)
        _emit_debug_event(
            settings,
            "browser_cookie_sync_complete",
            reason=reason,
            synced_cookie_count=synced_cookie_count,
            browser_cookie_count=len(browser_cookies),
            **fastmoss.cookie_snapshot(),
        )
        if retry_probe is None:
            page.remove_listener("response", response_listener)
            return synced_cookie_count

        _emit_debug_event(
            settings,
            "browser_recovery_wait_start",
            reason=reason,
            timeout_seconds=retry_wait_timeout_seconds,
            poll_interval_seconds=retry_poll_interval_seconds,
        )
        recovered = _wait_for_browser_captcha_recovery(
            page=page,
            signals=signals,
            timeout_seconds=retry_wait_timeout_seconds,
            poll_interval_seconds=retry_poll_interval_seconds,
        )
        _emit_debug_event(
            settings,
            "browser_recovery_wait_end",
            reason=reason,
            recovered=recovered,
            captcha_config_seen=signals.captcha_config_seen,
            captcha_verify_ok=signals.captcha_verify_ok,
            core_success_paths=list(signals.core_success_paths),
        )
        browser_cookies = page.context.cookies(["https://www.fastmoss.com"])
        fastmoss.replace_browser_cookies(browser_cookies)
        page.remove_listener("response", response_listener)
        if recovered:
            _emit_debug_event(
                settings,
                "browser_recovery_retry_probe",
                reason=reason,
                **fastmoss.cookie_snapshot(),
            )
            return retry_probe()

    if last_error is not None:
        raise last_error
    raise FastMossHTTPError(
        f"Timed out waiting for browser session recovery after {reason}",
        stage="browser.session_recovery",
        method="GET",
        path="/api/captcha/verify",
    )


def _call_fastmoss_with_browser_cookie_recovery(
    *,
    fastmoss: FastMossHTTPSession,
    settings: Mapping[str, Any],
    action: Callable[[], dict[str, Any]],
    product_id: str = "",
    author_uid: str = "",
    before_browser_wait: Callable[[], None] | None = None,
    after_browser_wait: Callable[[], None] | None = None,
) -> dict[str, Any]:
    try:
        return action()
    except FastMossHTTPError as exc:
        _emit_debug_event(
            settings,
            "fastmoss_request_failed",
            stage=exc.stage,
            method=exc.method,
            path=exc.path,
            response_code=str(exc.response_code or ""),
            error_type=_classify_fastmoss_error_type(exc),
        )
        raise


def _build_browser_session_kwargs(settings: Mapping[str, Any]) -> dict[str, Any] | None:
    profile_id = str(settings.get("browser_profile_id", "") or "").strip()
    provider_name = str(settings.get("browser_provider_name", "") or "").strip()
    profile_ref = str(settings.get("profile_ref", "") or "").strip()
    workspace_id_value = settings.get("browser_workspace_id")

    if profile_id and provider_name and workspace_id_value not in (None, ""):
        try:
            workspace_id = int(workspace_id_value)
        except (TypeError, ValueError):
            workspace_id = 0
        if workspace_id > 0:
            return {
                "workspace_id": workspace_id,
                "profile_id": profile_id,
                "provider_name": provider_name,
            }

    if profile_ref:
        return {"profile_ref": profile_ref}
    return None


def _has_browser_cookie_source(settings: Mapping[str, Any]) -> bool:
    return _build_browser_session_kwargs(settings) is not None


def _build_browser_recovery_url(*, product_id: str, author_uid: str) -> str:
    normalized_product_id = str(product_id or "").strip()
    if normalized_product_id:
        return f"https://www.fastmoss.com/zh/e-commerce/detail/{normalized_product_id}"
    normalized_author_uid = str(author_uid or "").strip()
    if normalized_author_uid:
        return f"https://www.fastmoss.com/zh/influencer/detail/{normalized_author_uid}"
    return "https://www.fastmoss.com/zh/account/center"


def _build_browser_captcha_response_listener(
    signals: BrowserCaptchaSignals,
    *,
    event_callback: Callable[..., None] | None = None,
) -> Callable[[Any], None]:
    def _listener(response: Any) -> None:
        try:
            url = str(response.url or "")
        except Exception:
            return
        path = url
        if "fastmoss.com" in url:
            path = "/" + url.split("fastmoss.com/", 1)[-1].split("?", 1)[0].lstrip("/")
        try:
            payload = response.json()
        except Exception:
            payload = {}
        code = ""
        if isinstance(payload, Mapping):
            code = str(payload.get("code") or "").strip()
        if callable(event_callback):
            ext = payload.get("ext") if isinstance(payload, Mapping) else {}
            ext_is_login = ""
            if isinstance(ext, Mapping):
                ext_is_login = str(ext.get("is_login") or "").strip()
            try:
                event_callback(
                    "browser_network_response",
                    path=path,
                    response_code=code,
                    status_code=getattr(response, "status", None),
                    ext_is_login=ext_is_login,
                )
            except Exception:
                pass

        if path == "/api/captcha/config":
            signals.captcha_config_seen = True
        elif path == "/api/captcha/verify" and code in {"200", "0"}:
            signals.captcha_verify_ok = True
        elif path in {"/api/goods/v3/base", "/api/goods/v3/author", "/api/goods/v3/overview"} and code in {"200", "0"}:
            signals.add_core_success(path)

    return _listener


def _wait_for_browser_captcha_recovery(
    *,
    page: Any,
    signals: BrowserCaptchaSignals,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> bool:
    deadline = time.monotonic() + max(timeout_seconds, 1.0)
    while time.monotonic() < deadline:
        if signals.is_recovered():
            return True
        time.sleep(max(poll_interval_seconds, 0.2))
    return signals.is_recovered()


def _should_retry_via_browser_cookie_refresh(exc: FastMossHTTPError) -> bool:
    error_type = _classify_fastmoss_error_type(exc)
    code = str(exc.response_code or "").strip()
    if error_type == "fastmoss_risk_control":
        return True
    return code in {"MAG_AUTH_3001", "MAG_AUTH_3002", "MSG_30001"}


def _browser_cookie_list_has_fd_tk(cookies: list[Mapping[str, Any]]) -> bool:
    for cookie in cookies:
        if str(cookie.get("name") or "").strip() != "fd_tk":
            continue
        if str(cookie.get("value") or "").strip():
            return True
    return False


def _fetch_author_bundle_with_recovery(
    *,
    fastmoss: FastMossHTTPSession,
    settings: Mapping[str, Any],
    product_id: str,
    uid: str | None,
    unique_id: str,
    author_uid: str,
    include_contact: bool,
    before_browser_wait: Callable[[], None] | None,
    after_browser_wait: Callable[[], None] | None,
) -> dict[str, Any]:
    resolved_uid = str(uid or "").strip()
    if not resolved_uid:
        resolved_uid = _call_fastmoss_with_browser_cookie_recovery(
            fastmoss=fastmoss,
            settings=settings,
            action=lambda: {"uid": fastmoss.resolve_author_uid(uid=uid, unique_id=unique_id)},
            product_id=product_id,
            author_uid=author_uid,
            before_browser_wait=before_browser_wait,
            after_browser_wait=after_browser_wait,
        ).get("uid", "")

    base_info = fastmoss.get_author_base_info(resolved_uid)
    author_index = fastmoss.get_author_index(resolved_uid)
    cargo_summary = fastmoss.get_author_cargo_summary(resolved_uid)
    bundle: dict[str, Any] = {
        "uid": resolved_uid,
        "unique_id": str(base_info.get("unique_id") or unique_id or ""),
        "base_info": base_info,
        "author_index": author_index,
        "cargo_summary": cargo_summary,
    }
    bundle["shop_list"] = fastmoss.get_author_shop_list(resolved_uid)
    if include_contact:
        bundle["author_contact"] = fastmoss.get_author_contact(resolved_uid)
    return bundle


def _run_single_row_worker_via_queue(
    *,
    params: Mapping[str, Any],
    raw_record: Mapping[str, Any],
    settings: Mapping[str, Any],
    queue_settings: Mapping[str, Any],
    source_target: TableTarget,
    target_target: TableTarget,
    target_schema: Mapping[str, Any],
    influencer_index: dict[str, dict[str, Any]],
    fastmoss: FastMossHTTPSession,
    store: Phase1RuntimeStore,
    execution: SyntheticExecutionContext,
) -> dict[str, Any]:
    record_id = str(raw_record.get("record_id", "") or "").strip()
    fields = raw_record.get("fields")
    product_id = ""
    if isinstance(fields, Mapping):
        product_id = str(fields.get(DEFAULT_COMPETITOR_PRODUCT_ID_FIELD_NAME, "") or "").strip()

    enqueue_payload = store.enqueue_task_executions(
        request_id=str(execution.request_id or execution.execution_id),
        item_code=INFLUENCER_POOL_SINGLE_ROW_TASK_NAME,
        workflow_code="sync_tk_influencer_pool_single_row_v1",
        items=[
            {
                "business_key": record_id or product_id,
                "dedupe_key": f"{INFLUENCER_POOL_SINGLE_ROW_TASK_NAME}:{execution.run_id}:{record_id}:{product_id}",
                "resource_code": str(queue_settings["resource_code"]),
                "max_attempts": 3,
                "payload": _build_single_row_worker_payload(
                    params=params,
                    raw_record=raw_record,
                    settings=settings,
                ),
            }
        ],
    )
    created_records = enqueue_payload.get("created_records") or []
    created_execution_id = ""
    if created_records:
        created_execution_id = str(created_records[0].get("execution_id", "") or "").strip()
    claimed_execution = _claim_single_row_worker_snapshot(
        store=store,
        queue_settings=queue_settings,
        execution_id=created_execution_id,
    )
    if claimed_execution is None:
        item = {
            "record_id": record_id,
            "product_id": product_id,
            "status": "failed_retry",
            "error": f"Worker queue claim timed out after {queue_settings['wait_timeout_seconds']}s",
            "matched_author_count": 0,
            "synced_author_count": 0,
            "already_synced_author_count": 0,
            "skipped_author_count": 0,
            "target_influencer_ids": [],
            "already_synced_influencer_ids": [],
            "failed_influencers": [],
            "worker": {
                "request_id": str(execution.request_id or execution.execution_id),
                "execution_id": created_execution_id,
                "worker_id": "",
                "resource_code": str(queue_settings["resource_code"]),
                "delay_seconds": [
                    float(settings["request_delay_min_seconds"]),
                    float(settings["request_delay_max_seconds"]),
                ],
            },
        }
        if created_execution_id:
            store.mark_browser_execution_retry_or_failed(
                execution_id=created_execution_id,
                run_id=f"worker-timeout-{created_execution_id}",
                error_text=item["error"],
                result=item,
                summary={"total": 1, "counts": {"failed_retry": 1}},
                retry_delay_seconds=float(queue_settings["retry_delay_seconds"]),
            )
        _update_source_sync_status(
            source_target=source_target,
            record_id=record_id,
            status_value="失败重试",
            apply_mutations=bool(settings["apply_mutations"]),
        )
        return item

    return _execute_single_row_worker_snapshot(
        raw_record=raw_record,
        settings=settings,
        queue_settings=queue_settings,
        claimed_execution=claimed_execution,
        source_target=source_target,
        target_target=target_target,
        target_schema=target_schema,
        influencer_index=influencer_index,
        fastmoss=fastmoss,
        store=store,
        execution=execution,
    )


def _build_single_row_worker_payload(
    *,
    params: Mapping[str, Any],
    raw_record: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    record_id = str(raw_record.get("record_id", "") or "").strip()
    fields = raw_record.get("fields")
    product_id = ""
    if isinstance(fields, Mapping):
        product_id = str(fields.get(DEFAULT_COMPETITOR_PRODUCT_ID_FIELD_NAME, "") or "").strip()
    return {
        "table_url": str(settings["table_url"]),
        "target_table_url": str(settings["target_table_url"]),
        "record_id": record_id,
        "product_id": product_id,
        "include_contact": bool(settings["include_contact"]),
        "max_author_pages": int(settings["max_author_pages"]) or 0,
        "request_delay_min_seconds": float(settings["request_delay_min_seconds"]),
        "request_delay_max_seconds": float(settings["request_delay_max_seconds"]),
        "run_mode": str(settings["run_mode"]),
        "params_source": {
            key: params.get(key)
            for key in (
                "access_token_env",
                "fastmoss_phone_env",
                "fastmoss_password_env",
                "requested_by",
                "execution_requested_by",
            )
            if key in params
        },
    }


def _claim_single_row_worker_snapshot(
    *,
    store: Phase1RuntimeStore,
    queue_settings: Mapping[str, Any],
    execution_id: str,
):
    if not execution_id:
        return None
    deadline = time.monotonic() + float(queue_settings["wait_timeout_seconds"])
    while time.monotonic() < deadline:
        execution = store.claim_browser_execution(
            execution_id=execution_id,
            worker_id=str(queue_settings["worker_id"]),
            lease_seconds=float(queue_settings["lease_seconds"]),
        )
        if execution is not None:
            return execution
        time.sleep(float(queue_settings["poll_interval_seconds"]))
    return None


def _execute_single_row_worker_snapshot(
    *,
    raw_record: Mapping[str, Any],
    settings: Mapping[str, Any],
    queue_settings: Mapping[str, Any],
    claimed_execution: Any,
    source_target: TableTarget,
    target_target: TableTarget,
    target_schema: Mapping[str, Any],
    influencer_index: dict[str, dict[str, Any]],
    fastmoss: FastMossHTTPSession,
    store: Phase1RuntimeStore,
    execution: SyntheticExecutionContext,
) -> dict[str, Any]:
    run_id = str(claimed_execution.run_id or f"worker-{claimed_execution.execution_id}")
    try:
        with _BrowserExecutionHeartbeat(
            store=store,
            execution_id=str(claimed_execution.execution_id),
            lease_seconds=float(queue_settings["lease_seconds"]),
            interval_seconds=float(
                min(queue_settings["heartbeat_interval_seconds"], queue_settings["lease_seconds"])
            ),
        ):
            item = _process_source_record(
                raw_record=raw_record,
                source_target=source_target,
                target_target=target_target,
                target_schema=target_schema,
                influencer_index=influencer_index,
                fastmoss=fastmoss,
                store=store,
                execution=execution,
                apply_mutations=bool(settings["apply_mutations"]),
                include_contact=bool(settings["include_contact"]),
                max_author_pages=int(settings["max_author_pages"]) or None,
                settings=settings,
            )
        item["worker"] = {
            "request_id": str(claimed_execution.request_id),
            "execution_id": str(claimed_execution.execution_id),
            "worker_id": str(claimed_execution.worker_id or queue_settings["worker_id"]),
            "resource_code": str(claimed_execution.resource_code),
            "delay_seconds": [
                float(settings["request_delay_min_seconds"]),
                float(settings["request_delay_max_seconds"]),
            ],
        }
        status = str(item.get("status", "") or "")
        summary = {"total": 1, "counts": {status or "unknown": 1}}
        if status in {"completed", "completed_no_matches", "skipped_unavailable"}:
            store.mark_browser_execution_success(
                execution_id=str(claimed_execution.execution_id),
                run_id=run_id,
                summary=summary,
                result=item,
            )
        else:
            store.mark_browser_execution_retry_or_failed(
                execution_id=str(claimed_execution.execution_id),
                run_id=run_id,
                error_text=str(item.get("error", "") or "worker failed"),
                summary=summary,
                result=item,
                retry_delay_seconds=float(queue_settings["retry_delay_seconds"]),
            )
        return item
    except Exception as exc:
        item = {
            "record_id": str(raw_record.get("record_id", "") or "").strip(),
            "product_id": str((raw_record.get("fields") or {}).get(DEFAULT_COMPETITOR_PRODUCT_ID_FIELD_NAME, "") or "").strip()
            if isinstance(raw_record.get("fields"), Mapping)
            else "",
            "status": "failed_retry",
            "error": str(exc),
            "matched_author_count": 0,
            "synced_author_count": 0,
            "already_synced_author_count": 0,
            "skipped_author_count": 0,
            "target_influencer_ids": [],
            "already_synced_influencer_ids": [],
            "failed_influencers": [],
            "worker": {
                "request_id": str(claimed_execution.request_id),
                "execution_id": str(claimed_execution.execution_id),
                "worker_id": str(claimed_execution.worker_id or queue_settings["worker_id"]),
                "resource_code": str(claimed_execution.resource_code),
                "delay_seconds": [
                    float(settings["request_delay_min_seconds"]),
                    float(settings["request_delay_max_seconds"]),
                ],
            },
        }
        store.mark_browser_execution_retry_or_failed(
            execution_id=str(claimed_execution.execution_id),
            run_id=run_id,
            error_text=str(exc),
            summary={"total": 1, "counts": {"failed_retry": 1}},
            result=item,
            retry_delay_seconds=float(queue_settings["retry_delay_seconds"]),
        )
        return item


def _run_one_influencer_pool_product_worker(
    *,
    settings: Mapping[str, Any],
    queue_settings: Mapping[str, Any],
    source_target: TableTarget,
    target_target: TableTarget,
    target_schema: Mapping[str, Any],
    influencer_index: dict[str, dict[str, Any]],
    fastmoss: FastMossHTTPSession,
    store: Phase1RuntimeStore,
    execution: SyntheticExecutionContext,
) -> dict[str, Any] | None:
    if not hasattr(store, "claim_influencer_pool_product_job"):
        return None
    product_job = store.claim_influencer_pool_product_job(
        worker_id=str(queue_settings["worker_id"]),
        lease_seconds=float(queue_settings["lease_seconds"]),
    )
    if not product_job:
        return None

    raw_record = product_job.get("source_record")
    if not isinstance(raw_record, Mapping) or not raw_record:
        failure_item = {
            "worker_kind": "product",
            "product_job_id": str(product_job.get("job_id", "") or ""),
            "record_id": str(product_job.get("source_record_id", "") or ""),
            "product_id": str(product_job.get("product_id", "") or ""),
            "status": "failed_retry",
            "error": "Product job source_record is empty.",
            "error_type": "input_validation",
        }
        _mark_product_job_failed_from_item(
            store=store,
            product_job=product_job,
            item=failure_item,
            run_id=str(execution.run_id or ""),
            retry_delay_seconds=float(queue_settings["retry_delay_seconds"]),
        )
        return failure_item

    product_settings = dict(settings)
    product_settings["drain_author_detail_jobs_inline"] = False
    item = _process_source_record(
        raw_record=raw_record,
        source_target=source_target,
        target_target=target_target,
        target_schema=target_schema,
        influencer_index=influencer_index,
        fastmoss=fastmoss,
        store=store,
        execution=execution,
        apply_mutations=bool(settings["apply_mutations"]),
        include_contact=bool(settings["include_contact"]),
        max_author_pages=int(settings["max_author_pages"]) or None,
        settings=product_settings,
    )
    item["worker_kind"] = "product"
    item["product_job_id"] = str(product_job.get("job_id", "") or "")

    if item.get("status") in {"completed", "completed_no_matches", "skipped_unavailable"}:
        store.mark_influencer_pool_product_job_success(
            job_id=str(product_job["job_id"]),
            run_id=str(execution.run_id or ""),
            stage=str(item.get("status") or "completed"),
        )
    elif item.get("status") == "detail_pending":
        author_queue_summary = item.get("author_queue_summary")
        if not isinstance(author_queue_summary, Mapping):
            author_queue_summary = {}
        store.mark_influencer_pool_product_job_discovered(
            job_id=str(product_job["job_id"]),
            run_id=str(execution.run_id or ""),
            matched_author_count=_coerce_count(item.get("matched_author_count")),
            queued_author_job_count=_coerce_count(
                author_queue_summary.get("total") or author_queue_summary.get("queued_author_job_count")
            ),
        )
    else:
        _mark_product_job_failed_from_item(
            store=store,
            product_job=product_job,
            item=item,
            run_id=str(execution.run_id or ""),
            retry_delay_seconds=float(queue_settings["retry_delay_seconds"]),
        )
    return item


def _run_one_influencer_pool_author_worker(
    *,
    settings: Mapping[str, Any],
    queue_settings: Mapping[str, Any],
    source_target: TableTarget,
    target_target: TableTarget,
    target_schema: Mapping[str, Any],
    influencer_index: dict[str, dict[str, Any]],
    fastmoss: FastMossHTTPSession,
    store: Phase1RuntimeStore,
    execution: SyntheticExecutionContext,
) -> dict[str, Any] | None:
    author_job = _claim_next_author_detail_job(
        store=store,
        product_id="",
        source_record_id="",
        worker_id=str(queue_settings["worker_id"]),
        lease_seconds=float(queue_settings["lease_seconds"]),
        fallback_jobs=[],
    )
    if not author_job:
        return None

    product_id = str(author_job.get("product_id", "") or "")
    record_id = str(author_job.get("source_record_id", "") or "")
    influencer_id = str(author_job.get("influencer_id", "") or "")
    item: dict[str, Any] = {
        "worker_kind": "author",
        "author_job_id": str(author_job.get("job_id", "") or ""),
        "record_id": record_id,
        "product_id": product_id,
        "influencer_id": influencer_id,
        "status": "",
        "matched_author_count": 1,
        "synced_author_count": 0,
        "created_author_count": 0,
        "updated_author_count": 0,
        "already_synced_author_count": 0,
        "skipped_author_count": 0,
        "target_influencer_ids": [],
        "already_synced_influencer_ids": [],
        "non_blocking_failures": [],
        "failed_influencers": [],
        "hard_stop": False,
        "hard_stop_reason": "",
        "hard_stop_code": "",
    }
    browser_waiting = False

    def _mark_browser_waiting() -> None:
        nonlocal browser_waiting
        if browser_waiting:
            return
        browser_waiting = True
        _update_source_sync_status(
            source_target=source_target,
            record_id=record_id,
            status_value=SOURCE_STATUS_AWAITING_CAPTCHA_VALUE,
            apply_mutations=bool(settings["apply_mutations"]),
        )

    def _mark_browser_resumed() -> None:
        nonlocal browser_waiting
        if not browser_waiting:
            return
        browser_waiting = False
        _update_source_sync_status(
            source_target=source_target,
            record_id=record_id,
            status_value="处理中",
            apply_mutations=bool(settings["apply_mutations"]),
        )

    try:
        outcome = _process_author_detail_job(
            author_job=author_job,
            product_id=product_id,
            record_id=record_id,
            source_images=author_job.get("source_images"),
            holiday_name=str(author_job.get("holiday_name", "") or ""),
            include_contact=bool(settings["include_contact"]),
            apply_mutations=bool(settings["apply_mutations"]),
            existing_state=influencer_index.get(influencer_id),
            target_target=target_target,
            target_schema=target_schema,
            fastmoss=fastmoss,
            store=store,
            execution=execution,
            settings=settings,
            before_browser_wait=_mark_browser_waiting,
            after_browser_wait=_mark_browser_resumed,
        )
        item["non_blocking_failures"] = [dict(warning) for warning in outcome["non_blocking_failures"]]
        if outcome["action"] == "created":
            item["created_author_count"] = 1
            item["synced_author_count"] = 1
            item["target_influencer_ids"] = [influencer_id]
        elif outcome["action"] == "updated":
            item["updated_author_count"] = 1
            item["synced_author_count"] = 1
            item["target_influencer_ids"] = [influencer_id]
        elif outcome["action"] == "skipped_checkpoint":
            item["already_synced_author_count"] = 1
            item["already_synced_influencer_ids"] = [influencer_id]
            _mark_author_detail_job_skipped(
                store=store,
                author_job=author_job,
                run_id=str(execution.run_id or ""),
                stage="checkpoint",
                reason="source_product_ids already contains product_id",
            )
        merged_state = outcome["merged_state"]
        if outcome["action"] != "skipped_checkpoint":
            influencer_index[influencer_id] = merge_influencer_facts(
                influencer_index.get(influencer_id),
                merged_state,
            )
            _mark_author_detail_job_success(
                store=store,
                author_job=author_job,
                run_id=str(execution.run_id or ""),
                target_record_id=str(merged_state.get("target_record_id") or ""),
                snapshot_id=str(
                    (merged_state.get("entity_snapshot") or {}).get("snapshot_id")
                    if isinstance(merged_state.get("entity_snapshot"), Mapping)
                    else ""
                ),
            )
        _reactivate_product_finalizer(
            store=store,
            source_record_id=record_id,
            product_id=product_id,
            run_id=str(execution.run_id or ""),
        )
        item["status"] = "completed"
        return item
    except Exception as exc:
        failure_detail = _build_influencer_failure_detail(
            exc=exc,
            influencer_id=influencer_id,
            default_stage="author.bundle",
        )
        item["status"] = "failed_retry"
        item["error"] = failure_detail["error"]
        item["error_type"] = failure_detail["error_type"]
        item["failed_influencers"] = [failure_detail]
        if isinstance(exc, FastMossHTTPError) and _is_fastmoss_hard_stop_error(exc):
            item["hard_stop"] = True
            item["hard_stop_reason"] = item["error_type"]
            item["hard_stop_code"] = str(exc.response_code or "")
        _mark_author_detail_job_failed(
            store=store,
            author_job=author_job,
            run_id=str(execution.run_id or ""),
            failure_detail=failure_detail,
            retry_delay_seconds=float(queue_settings["retry_delay_seconds"]),
        )
        _update_source_sync_status(
            source_target=source_target,
            record_id=record_id,
            status_value="失败重试",
            apply_mutations=bool(settings["apply_mutations"]),
        )
        _reactivate_product_finalizer(
            store=store,
            source_record_id=record_id,
            product_id=product_id,
            run_id=str(execution.run_id or ""),
        )
        return item


def _run_one_influencer_pool_finalizer(
    *,
    settings: Mapping[str, Any],
    source_target: TableTarget,
    store: Phase1RuntimeStore,
    execution: SyntheticExecutionContext,
    limit: int,
) -> dict[str, Any] | None:
    if not hasattr(store, "list_influencer_pool_product_jobs_for_finalizer"):
        return None
    product_jobs = store.list_influencer_pool_product_jobs_for_finalizer(limit=limit)
    for product_job in product_jobs:
        record_id = str(product_job.get("source_record_id", "") or "")
        product_id = str(product_job.get("product_id", "") or "")
        summary = _summarize_author_detail_jobs(
            store=store,
            product_id=product_id,
            source_record_id=record_id,
            fallback_jobs=[],
        )
        pending_count = _coerce_count(summary.get("pending_count")) + _coerce_count(summary.get("running_count"))
        retry_count = _coerce_count(summary.get("failed_retry_count")) + _coerce_count(summary.get("hard_failed_count"))
        if pending_count > 0:
            continue
        item = {
            "worker_kind": "finalizer",
            "product_job_id": str(product_job.get("job_id", "") or ""),
            "record_id": record_id,
            "product_id": product_id,
            "author_queue_summary": summary,
            "matched_author_count": _coerce_count(product_job.get("matched_author_count")),
            "synced_author_count": _coerce_count(summary.get("succeeded_count")),
            "created_author_count": 0,
            "updated_author_count": 0,
            "already_synced_author_count": _coerce_count(summary.get("skipped_count")),
            "skipped_author_count": 0,
            "target_influencer_ids": [],
            "already_synced_influencer_ids": [],
            "failed_influencers": [],
        }
        if retry_count > 0:
            item.update(
                {
                    "status": "failed_retry",
                    "error": "Author detail jobs are waiting for retry.",
                    "error_type": "author_detail_jobs_failed_retry",
                }
            )
            _update_source_sync_status(
                source_target=source_target,
                record_id=record_id,
                status_value="失败重试",
                apply_mutations=bool(settings["apply_mutations"]),
            )
            if hasattr(store, "mark_influencer_pool_product_job_author_retry_wait"):
                store.mark_influencer_pool_product_job_author_retry_wait(
                    job_id=str(product_job["job_id"]),
                    run_id=str(execution.run_id or ""),
                    error_text=item["error"],
                    error_type=item["error_type"],
                )
            return item

        item["status"] = "completed" if _coerce_count(summary.get("total")) > 0 else "completed_no_matches"
        _update_source_sync_status(
            source_target=source_target,
            record_id=record_id,
            status_value="已完成",
            apply_mutations=bool(settings["apply_mutations"]),
        )
        if hasattr(store, "mark_influencer_pool_product_job_success"):
            store.mark_influencer_pool_product_job_success(
                job_id=str(product_job["job_id"]),
                run_id=str(execution.run_id or ""),
                stage=str(item["status"]),
            )
        return item
    return None


def _process_source_record(
    *,
    raw_record: Mapping[str, Any],
    source_target: TableTarget,
    target_target: TableTarget,
    target_schema: Mapping[str, Any],
    influencer_index: dict[str, dict[str, Any]],
    fastmoss: FastMossHTTPSession,
    store: Phase1RuntimeStore,
    execution: SyntheticExecutionContext,
    apply_mutations: bool,
    include_contact: bool,
    max_author_pages: int | None,
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    record_id = str(raw_record.get("record_id", "") or "").strip()
    fields = raw_record.get("fields")
    if not isinstance(fields, Mapping):
        fields = {}

    product_id = str(fields.get(DEFAULT_COMPETITOR_PRODUCT_ID_FIELD_NAME, "") or "").strip()
    source_status = _normalize_status_field_value(fields.get(DEFAULT_COMPETITOR_SYNC_STATUS_FIELD_NAME))
    product_status = _normalize_status_field_value(fields.get(DEFAULT_COMPETITOR_STATUS_FIELD_NAME))
    holiday_name = _extract_single_name(fields.get(DEFAULT_COMPETITOR_HOLIDAY_FIELD_NAME))
    source_images = fields.get(DEFAULT_COMPETITOR_IMAGE_FIELD_NAME)

    result_item: dict[str, Any] = {
        "record_id": record_id,
        "product_id": product_id,
        "source_status": source_status,
        "product_status": product_status,
        "force_full_refresh": False,
        "status": "",
        "error": "",
        "error_type": "",
        "matched_author_count": 0,
        "synced_author_count": 0,
        "created_author_count": 0,
        "updated_author_count": 0,
        "already_synced_author_count": 0,
        "skipped_author_count": 0,
        "target_influencer_ids": [],
        "already_synced_influencer_ids": [],
        "non_blocking_failures": [],
        "failed_influencers": [],
        "hard_stop": False,
        "hard_stop_reason": "",
        "hard_stop_code": "",
    }
    matched_author_count = 0
    synced_author_count = 0
    created_author_count = 0
    updated_author_count = 0
    already_synced_author_count = 0
    skipped_author_count = 0
    target_influencer_ids: list[str] = []
    already_synced_influencer_ids: list[str] = []
    non_blocking_failures: list[dict[str, Any]] = []
    failed_influencers: list[dict[str, Any]] = []
    force_full_refresh = False
    fastmoss.set_debug_context(record_id=record_id, product_id=product_id)
    _emit_debug_event(
        settings,
        "source_record_start",
        record_id=record_id,
        product_id=product_id,
        source_status=source_status,
        product_status=product_status,
    )

    try:
        if product_status == UNAVAILABLE_PRODUCT_STATUS_VALUE:
            result_item["status"] = "skipped_unavailable"
            _emit_debug_event(
                settings,
                "source_record_skipped_unavailable",
                record_id=record_id,
                product_id=product_id,
            )
            return result_item
        if not product_id:
            result_item["status"] = "failed_retry"
            result_item["error"] = "SKU-ID is empty"
            result_item["error_type"] = "input_validation"
            _update_source_sync_status(
                source_target=source_target,
                record_id=record_id,
                status_value="失败重试",
                apply_mutations=apply_mutations,
            )
            _emit_debug_event(
                settings,
                "source_record_failed_input_validation",
                record_id=record_id,
                error=result_item["error"],
            )
            return result_item
        force_full_refresh = _source_record_requests_full_refresh(
            source_status=source_status,
            product_id=product_id,
            influencer_index=influencer_index,
        )
        result_item["force_full_refresh"] = force_full_refresh
        if force_full_refresh:
            _emit_debug_event(
                settings,
                "source_record_force_full_refresh_requested",
                record_id=record_id,
                product_id=product_id,
                source_status=source_status,
            )

        _update_source_sync_status(
            source_target=source_target,
            record_id=record_id,
            status_value="处理中",
            apply_mutations=apply_mutations,
        )
        _emit_debug_event(
            settings,
            "source_status_transition",
            record_id=record_id,
            product_id=product_id,
            status_value="处理中",
        )
        seen_influencer_ids: set[str] = set()
        author_detail_jobs: list[dict[str, Any]] = []
        browser_waiting = False

        def _mark_browser_waiting() -> None:
            nonlocal browser_waiting
            if browser_waiting:
                return
            browser_waiting = True
            _update_source_sync_status(
                source_target=source_target,
                record_id=record_id,
                status_value=SOURCE_STATUS_AWAITING_CAPTCHA_VALUE,
                apply_mutations=apply_mutations,
            )
            _emit_debug_event(
                settings,
                "source_status_transition",
                record_id=record_id,
                product_id=product_id,
                status_value=SOURCE_STATUS_AWAITING_CAPTCHA_VALUE,
            )

        def _mark_browser_resumed() -> None:
            nonlocal browser_waiting
            if not browser_waiting:
                return
            browser_waiting = False
            _update_source_sync_status(
                source_target=source_target,
                record_id=record_id,
                status_value="处理中",
                apply_mutations=apply_mutations,
            )
            _emit_debug_event(
                settings,
                "source_status_transition",
                record_id=record_id,
                product_id=product_id,
                status_value="处理中",
            )

        page = 1
        pagesize = 10
        seen_rows = 0
        while True:
            payload = _call_fastmoss_with_browser_cookie_recovery(
                fastmoss=fastmoss,
                settings=settings,
                action=lambda: fastmoss.list_product_authors(
                    product_id,
                    page=page,
                    pagesize=pagesize,
                ),
                product_id=product_id,
                before_browser_wait=_mark_browser_waiting,
                after_browser_wait=_mark_browser_resumed,
            )
            payload_data = payload.get("data") if isinstance(payload, Mapping) else {}
            rows = payload_data.get("list") if isinstance(payload_data, Mapping) else None
            if not isinstance(rows, list) or not rows:
                break

            page_has_sales_candidate = False
            for author_row in rows:
                influencer_id = str(author_row.get("unique_id") or "").strip()
                if not influencer_id or influencer_id in seen_influencer_ids:
                    continue
                seen_influencer_ids.add(influencer_id)

                sold_count = _coerce_number(author_row.get("sold_count"))
                if sold_count <= 50:
                    skipped_author_count += 1
                    continue

                page_has_sales_candidate = True
                list_follower_count = _extract_author_list_follower_count(author_row)
                if list_follower_count <= 5000:
                    skipped_author_count += 1
                    _emit_debug_event(
                        settings,
                        "influencer_list_filter_skip",
                        record_id=record_id,
                        product_id=product_id,
                        influencer_id=influencer_id,
                        reason="low_or_missing_follower_count",
                        sold_count=sold_count,
                        follower_count=list_follower_count,
                    )
                    continue

                matched_author_count += 1
                existing_state = influencer_index.get(influencer_id)
                if not force_full_refresh and influencer_state_has_source_product(existing_state, product_id):
                    already_synced_author_count += 1
                    already_synced_influencer_ids.append(influencer_id)
                    _emit_debug_event(
                        settings,
                        "influencer_checkpoint_hit",
                        record_id=record_id,
                        product_id=product_id,
                        influencer_id=influencer_id,
                    )
                    continue

                uid = str(author_row.get("uid") or "").strip() or None
                author_detail_jobs.append(
                    _build_author_detail_job(
                        source_record_id=record_id,
                        product_id=product_id,
                        influencer_id=influencer_id,
                        uid=str(uid or ""),
                        sold_count=sold_count,
                        follower_count=list_follower_count,
                        holiday_name=holiday_name,
                        source_images=source_images,
                        author_row=author_row,
                        force_refresh=force_full_refresh,
                    )
                )

            seen_rows += len(rows)
            total = payload_data.get("total") if isinstance(payload_data, Mapping) else None
            if not page_has_sales_candidate:
                break
            if isinstance(total, int) and total > 0 and seen_rows >= total:
                break
            if len(rows) < pagesize:
                break
            page += 1
            if max_author_pages is not None and page > max_author_pages:
                break

        author_queue_payload = _upsert_author_detail_jobs(
            store=store,
            jobs=author_detail_jobs,
            force_full_refresh=force_full_refresh,
        )
        author_queue_summary = dict(author_queue_payload)
        if not _coerce_bool(settings.get("drain_author_detail_jobs_inline"), default=True):
            author_queue_summary = _summarize_author_detail_jobs(
                store=store,
                product_id=product_id,
                source_record_id=record_id,
                fallback_jobs=author_detail_jobs,
            ) | {
                "queued_author_job_count": len(author_detail_jobs),
                "processed_author_job_count": 0,
                **author_queue_summary,
            }
            if int(author_queue_summary.get("total") or 0) > 0:
                result_item.update(
                    {
                        "status": "detail_pending",
                        "matched_author_count": matched_author_count,
                        "synced_author_count": synced_author_count,
                        "created_author_count": created_author_count,
                        "updated_author_count": updated_author_count,
                        "already_synced_author_count": already_synced_author_count,
                        "skipped_author_count": skipped_author_count,
                        "target_influencer_ids": target_influencer_ids,
                        "already_synced_influencer_ids": already_synced_influencer_ids,
                        "non_blocking_failures": non_blocking_failures,
                        "failed_influencers": failed_influencers,
                        "author_queue_summary": author_queue_summary,
                    }
                )
                _emit_debug_event(
                    settings,
                    "source_record_author_jobs_queued",
                    record_id=record_id,
                    product_id=product_id,
                    matched_author_count=matched_author_count,
                    queued_author_job_count=len(author_detail_jobs),
                    author_queue_total=author_queue_summary.get("total"),
                )
                return result_item

        processed_author_job_count = 0
        max_author_detail_jobs = int(settings.get("max_author_detail_jobs_per_source_row") or 0)
        while max_author_detail_jobs <= 0 or processed_author_job_count < max_author_detail_jobs:
            author_job = _claim_next_author_detail_job(
                store=store,
                product_id=product_id,
                source_record_id=record_id,
                worker_id=str(getattr(execution, "run_id", "") or getattr(execution, "execution_id", "") or ""),
                lease_seconds=float(settings.get("execution_lease_seconds") or 60.0),
                fallback_jobs=author_detail_jobs,
            )
            if not author_job:
                break
            processed_author_job_count += 1
            trace_stage = "author.bundle"
            influencer_id = str(author_job.get("influencer_id", "") or "")
            try:
                outcome = _process_author_detail_job(
                    author_job=author_job,
                    product_id=product_id,
                    record_id=record_id,
                    source_images=author_job.get("source_images"),
                    holiday_name=str(author_job.get("holiday_name", "") or holiday_name),
                    include_contact=include_contact,
                    apply_mutations=apply_mutations,
                    existing_state=influencer_index.get(influencer_id),
                    target_target=target_target,
                    target_schema=target_schema,
                    fastmoss=fastmoss,
                    store=store,
                    execution=execution,
                    settings=settings,
                    before_browser_wait=_mark_browser_waiting,
                    after_browser_wait=_mark_browser_resumed,
                )
                for warning_payload in outcome["non_blocking_failures"]:
                    non_blocking_failures.append(warning_payload)
                    _emit_debug_event(
                        settings,
                        "influencer_non_blocking_failure",
                        record_id=record_id,
                        product_id=product_id,
                        influencer_id=warning_payload.get("influencer_id") or influencer_id,
                        stage=warning_payload.get("stage") or "",
                        field=warning_payload.get("field") or "",
                        url=warning_payload.get("url") or "",
                        error=warning_payload.get("error") or "",
                        resolution=warning_payload.get("resolution") or "",
                        status_code=warning_payload.get("status_code"),
                    )
                if outcome["action"] == "created":
                    created_author_count += 1
                elif outcome["action"] == "updated":
                    updated_author_count += 1
                elif outcome["action"] == "skipped_checkpoint":
                    already_synced_author_count += 1
                    already_synced_influencer_ids.append(influencer_id)
                merged_state = outcome["merged_state"]
                if outcome["action"] != "skipped_checkpoint":
                    influencer_index[influencer_id] = merge_influencer_facts(
                        influencer_index.get(influencer_id),
                        merged_state,
                    )
                synced_author_count = created_author_count + updated_author_count
                if outcome["action"] == "skipped_checkpoint":
                    _mark_author_detail_job_skipped(
                        store=store,
                        author_job=author_job,
                        run_id=str(getattr(execution, "run_id", "") or ""),
                        stage="checkpoint",
                        reason="source_product_ids already contains product_id",
                    )
                else:
                    target_influencer_ids.append(influencer_id)
                    _mark_author_detail_job_success(
                        store=store,
                        author_job=author_job,
                        run_id=str(getattr(execution, "run_id", "") or ""),
                        target_record_id=str(merged_state.get("target_record_id") or ""),
                        snapshot_id=str(
                            (merged_state.get("entity_snapshot") or {}).get("snapshot_id")
                            if isinstance(merged_state.get("entity_snapshot"), Mapping)
                            else ""
                        ),
                    )
            except Exception as exc:
                failure_detail = _build_influencer_failure_detail(
                    exc=exc,
                    influencer_id=influencer_id,
                    default_stage=trace_stage,
                )
                failed_influencers.append(failure_detail)
                _mark_author_detail_job_failed(
                    store=store,
                    author_job=author_job,
                    run_id=str(getattr(execution, "run_id", "") or ""),
                    failure_detail=failure_detail,
                    retry_delay_seconds=float(settings.get("execution_retry_delay_seconds") or 30.0),
                )
                _emit_debug_event(
                    settings,
                    "influencer_sync_failed",
                    record_id=record_id,
                    product_id=product_id,
                    influencer_id=failure_detail["influencer_id"],
                    stage=failure_detail["stage"],
                    field=failure_detail["field"],
                    url=failure_detail["url"],
                    error=failure_detail["error"],
                )
                raise

        author_queue_summary = _summarize_author_detail_jobs(
            store=store,
            product_id=product_id,
            source_record_id=record_id,
            fallback_jobs=author_detail_jobs,
        ) | {
            "queued_author_job_count": len(author_detail_jobs),
            "processed_author_job_count": processed_author_job_count,
            **author_queue_summary,
        }
        remaining_author_job_count = (
            int(author_queue_summary.get("pending_count") or 0)
            + int(author_queue_summary.get("running_count") or 0)
            + int(author_queue_summary.get("failed_retry_count") or 0)
        )
        if remaining_author_job_count > 0 and not failed_influencers:
            result_item.update(
                {
                    "status": "failed_retry",
                    "error": "Author detail jobs remain pending for this source product.",
                    "error_type": "author_detail_jobs_pending",
                    "matched_author_count": matched_author_count,
                    "synced_author_count": synced_author_count,
                    "created_author_count": created_author_count,
                    "updated_author_count": updated_author_count,
                    "already_synced_author_count": already_synced_author_count,
                    "skipped_author_count": skipped_author_count,
                    "target_influencer_ids": target_influencer_ids,
                    "already_synced_influencer_ids": already_synced_influencer_ids,
                    "non_blocking_failures": non_blocking_failures,
                    "failed_influencers": failed_influencers,
                    "author_queue_summary": author_queue_summary,
                }
            )
            _update_source_sync_status(
                source_target=source_target,
                record_id=record_id,
                status_value="失败重试",
                apply_mutations=apply_mutations,
            )
            return result_item

        final_status = (
            "completed"
            if synced_author_count > 0 or already_synced_author_count > 0
            else "completed_no_matches"
        )
        _update_source_sync_status(
            source_target=source_target,
            record_id=record_id,
            status_value="已完成",
            apply_mutations=apply_mutations,
        )
        _emit_debug_event(
            settings,
            "source_status_transition",
            record_id=record_id,
            product_id=product_id,
            status_value="已完成",
        )
        result_item.update(
            {
                "status": final_status,
                "matched_author_count": matched_author_count,
                "synced_author_count": synced_author_count,
                "created_author_count": created_author_count,
                "updated_author_count": updated_author_count,
                "already_synced_author_count": already_synced_author_count,
                "skipped_author_count": skipped_author_count,
                "target_influencer_ids": target_influencer_ids,
                "already_synced_influencer_ids": already_synced_influencer_ids,
                "non_blocking_failures": non_blocking_failures,
                "failed_influencers": failed_influencers,
                "author_queue_summary": author_queue_summary,
            }
        )
        _emit_debug_event(
            settings,
            "source_record_completed",
            record_id=record_id,
            product_id=product_id,
            status=final_status,
            matched_author_count=matched_author_count,
            synced_author_count=synced_author_count,
            already_synced_author_count=already_synced_author_count,
            skipped_author_count=skipped_author_count,
            force_full_refresh=force_full_refresh,
            non_blocking_failure_count=len(non_blocking_failures),
        )
        return result_item
    except Exception as exc:
        result_item.update(
            {
                "force_full_refresh": result_item.get("force_full_refresh", False),
                "matched_author_count": matched_author_count,
                "synced_author_count": synced_author_count,
                "created_author_count": created_author_count,
                "updated_author_count": updated_author_count,
                "already_synced_author_count": already_synced_author_count,
                "skipped_author_count": skipped_author_count,
                "target_influencer_ids": target_influencer_ids,
                "already_synced_influencer_ids": already_synced_influencer_ids,
                "non_blocking_failures": non_blocking_failures,
                "failed_influencers": failed_influencers,
            }
        )
        _update_source_sync_status(
            source_target=source_target,
            record_id=record_id,
            status_value="失败重试",
            apply_mutations=apply_mutations,
        )
        _emit_debug_event(
            settings,
            "source_status_transition",
            record_id=record_id,
            product_id=product_id,
            status_value="失败重试",
        )
        result_item["status"] = "failed_retry"
        if isinstance(exc, FastMossHTTPError):
            result_item["error"] = _format_fastmoss_error(exc)
            result_item["error_type"] = _classify_fastmoss_error_type(exc)
            if _is_fastmoss_hard_stop_error(exc):
                result_item["hard_stop"] = True
                result_item["hard_stop_reason"] = result_item["error_type"]
                result_item["hard_stop_code"] = str(exc.response_code or "")
            risk_control_hint = _detect_fastmoss_risk_control_hint(exc)
            if risk_control_hint:
                result_item["risk_control_hint"] = risk_control_hint
            result_item["fastmoss_error"] = exc.to_dict()
        else:
            result_item["error"] = str(exc)
            result_item["error_type"] = (
                str(failed_influencers[-1].get("error_type", "") or "unexpected")
                if failed_influencers
                else "unexpected"
            )
        _emit_debug_event(
            settings,
            "source_record_failed",
            record_id=record_id,
            product_id=product_id,
            error=result_item["error"],
            error_type=result_item["error_type"],
            failed_influencer_count=len(failed_influencers),
            failed_influencer_id=(
                str(failed_influencers[-1].get("influencer_id", "") or "")
                if failed_influencers
                else ""
            ),
            failure_stage=(
                str(failed_influencers[-1].get("stage", "") or "")
                if failed_influencers
                else ""
            ),
            failure_field=(
                str(failed_influencers[-1].get("field", "") or "")
                if failed_influencers
                else ""
            ),
            failure_url=(
                str(failed_influencers[-1].get("url", "") or "")
                if failed_influencers
                else ""
            ),
        )
        return result_item
    finally:
        fastmoss.clear_debug_context()


def _build_incoming_influencer_state(
    *,
    influencer_id: str,
    product_id: str,
    sold_count: float,
    holiday_name: str,
    source_images: Any,
    bundle: Mapping[str, Any],
) -> dict[str, Any]:
    base_info = dict(bundle.get("base_info") or {})
    author_index = dict(bundle.get("author_index") or {})
    cargo_summary = dict(bundle.get("cargo_summary") or {})
    shop_list_data = bundle.get("shop_list")
    if isinstance(shop_list_data, Mapping):
        shop_items = shop_list_data.get("list")
    else:
        shop_items = []
    author_contact = bundle.get("author_contact")
    return {
        "influencer_id": influencer_id,
        "source_product_ids": [product_id],
        "source_product_sales_by_id": {product_id: sold_count},
        "source_product_image_refs_by_id": {product_id: source_images},
        "holiday_names": [holiday_name] if holiday_name else [],
        "cooperation_shop_names": shop_items,
        "avatar": base_info.get("avatar") or "",
        "follower_count": author_index.get("follower_count") or author_index.get("fans_count") or "",
        "aweme_28_count": (
            author_index.get("aweme_28_count")
            or author_index.get("aweme_28d_count")
            or author_index.get("aweme_28_count_show")
            or ""
        ),
        "video_sale_amount": cargo_summary.get("video_sale_amount") or "",
        "live_sale_amount": cargo_summary.get("live_sale_amount") or "",
        "contact_text": format_influencer_contacts(author_contact),
    }


def _build_author_detail_job(
    *,
    source_record_id: str,
    product_id: str,
    influencer_id: str,
    uid: str,
    sold_count: float,
    follower_count: float,
    holiday_name: str,
    source_images: Any,
    author_row: Mapping[str, Any],
    force_refresh: bool = False,
) -> dict[str, Any]:
    return {
        "job_id": uuid.uuid4().hex,
        "source_record_id": source_record_id,
        "product_id": product_id,
        "influencer_id": influencer_id,
        "uid": uid,
        "sold_count": sold_count,
        "follower_count": follower_count,
        "holiday_name": holiday_name,
        "source_images": source_images,
        "author_row": dict(author_row),
        "force_refresh": bool(force_refresh),
        "status": "pending",
        "max_attempts": 3,
    }


def _build_product_job_from_source_record(raw_record: Mapping[str, Any]) -> dict[str, Any]:
    fields = raw_record.get("fields")
    product_id = ""
    if isinstance(fields, Mapping):
        product_id = str(fields.get(DEFAULT_COMPETITOR_PRODUCT_ID_FIELD_NAME, "") or "").strip()
    return {
        "source_record_id": str(raw_record.get("record_id", "") or "").strip(),
        "product_id": product_id,
        "source_record": dict(raw_record),
        "max_attempts": 3,
    }


def _upsert_product_jobs(
    *,
    store: Any,
    jobs: list[dict[str, Any]],
    force_refresh: bool,
) -> dict[str, Any]:
    if hasattr(store, "upsert_influencer_pool_product_jobs"):
        return dict(
            store.upsert_influencer_pool_product_jobs(
                jobs=jobs,
                force_refresh=force_refresh,
            )
        )
    return {
        "created_count": len(jobs),
        "updated_count": 0,
        "kept_terminal_count": 0,
    }


def _upsert_author_detail_jobs(
    *,
    store: Any,
    jobs: list[dict[str, Any]],
    force_full_refresh: bool,
) -> dict[str, Any]:
    if hasattr(store, "upsert_influencer_pool_author_jobs"):
        return dict(
            store.upsert_influencer_pool_author_jobs(
                jobs=jobs,
                force_refresh=force_full_refresh,
            )
        )
    for job in jobs:
        job.setdefault("status", "pending")
    return {
        "created_count": len(jobs),
        "updated_count": 0,
        "kept_terminal_count": 0,
    }


def _claim_next_author_detail_job(
    *,
    store: Any,
    product_id: str,
    source_record_id: str,
    worker_id: str,
    lease_seconds: float,
    fallback_jobs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if hasattr(store, "claim_influencer_pool_author_job"):
        return store.claim_influencer_pool_author_job(
            product_id=product_id,
            source_record_id=source_record_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
    for job in fallback_jobs:
        if job.get("status") in {"pending", "failed_retry"}:
            job["status"] = "running"
            return job
    return None


def _mark_author_detail_job_success(
    *,
    store: Any,
    author_job: Mapping[str, Any],
    run_id: str,
    target_record_id: str,
    snapshot_id: str,
) -> None:
    if hasattr(store, "mark_influencer_pool_author_job_success") and author_job.get("job_id"):
        store.mark_influencer_pool_author_job_success(
            job_id=str(author_job["job_id"]),
            run_id=run_id,
            target_record_id=target_record_id,
            snapshot_id=snapshot_id,
        )
        return
    if isinstance(author_job, dict):
        author_job["status"] = "succeeded"


def _mark_author_detail_job_skipped(
    *,
    store: Any,
    author_job: Mapping[str, Any],
    run_id: str,
    stage: str,
    reason: str,
) -> None:
    if hasattr(store, "mark_influencer_pool_author_job_skipped") and author_job.get("job_id"):
        store.mark_influencer_pool_author_job_skipped(
            job_id=str(author_job["job_id"]),
            run_id=run_id,
            stage=stage,
            reason=reason,
        )
        return
    if isinstance(author_job, dict):
        author_job["status"] = "skipped"
        author_job["last_error"] = {"stage": stage, "reason": reason}


def _mark_author_detail_job_failed(
    *,
    store: Any,
    author_job: Mapping[str, Any],
    run_id: str,
    failure_detail: Mapping[str, Any],
    retry_delay_seconds: float,
) -> None:
    if hasattr(store, "mark_influencer_pool_author_job_failed") and author_job.get("job_id"):
        store.mark_influencer_pool_author_job_failed(
            job_id=str(author_job["job_id"]),
            run_id=run_id,
            error_text=str(failure_detail.get("error", "") or ""),
            error_type=str(failure_detail.get("error_type", "") or ""),
            error_code=str(failure_detail.get("code", "") or ""),
            error_path=str(failure_detail.get("path") or failure_detail.get("url") or ""),
            stage=str(failure_detail.get("stage", "") or ""),
            retry_delay_seconds=retry_delay_seconds,
        )
        return
    if isinstance(author_job, dict):
        author_job["status"] = "failed_retry"
        author_job["last_error"] = dict(failure_detail)


def _summarize_author_detail_jobs(
    *,
    store: Any,
    product_id: str,
    source_record_id: str,
    fallback_jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    if hasattr(store, "summarize_influencer_pool_author_jobs"):
        return dict(
            store.summarize_influencer_pool_author_jobs(
                product_id=product_id,
                source_record_id=source_record_id,
            )
        )
    counts: dict[str, int] = {}
    for job in fallback_jobs:
        status = str(job.get("status", "") or "pending")
        counts[status] = counts.get(status, 0) + 1
    return {
        "total": sum(counts.values()),
        "counts": counts,
        "pending_count": counts.get("pending", 0),
        "running_count": counts.get("running", 0),
        "failed_retry_count": counts.get("failed_retry", 0),
        "succeeded_count": counts.get("succeeded", 0),
        "skipped_count": counts.get("skipped", 0),
        "hard_failed_count": counts.get("hard_failed", 0),
    }


def _mark_product_job_failed_from_item(
    *,
    store: Any,
    product_job: Mapping[str, Any],
    item: Mapping[str, Any],
    run_id: str,
    retry_delay_seconds: float,
) -> None:
    if not hasattr(store, "mark_influencer_pool_product_job_failed") or not product_job.get("job_id"):
        return
    failed_influencers = item.get("failed_influencers")
    failed_influencer = {}
    if isinstance(failed_influencers, list) and failed_influencers:
        first_failed = failed_influencers[0]
        if isinstance(first_failed, Mapping):
            failed_influencer = dict(first_failed)
    store.mark_influencer_pool_product_job_failed(
        job_id=str(product_job["job_id"]),
        run_id=run_id,
        error_text=str(item.get("error", "") or failed_influencer.get("error", "") or ""),
        error_type=str(item.get("error_type", "") or failed_influencer.get("error_type", "") or ""),
        error_code=str(item.get("hard_stop_code", "") or failed_influencer.get("code", "") or ""),
        error_path=str(failed_influencer.get("path") or failed_influencer.get("url") or ""),
        stage=str(failed_influencer.get("stage") or item.get("error_type") or ""),
        retry_delay_seconds=retry_delay_seconds,
        hard_stop=_item_requests_hard_stop(item),
    )


def _reactivate_product_finalizer(
    *,
    store: Any,
    source_record_id: str,
    product_id: str,
    run_id: str,
) -> None:
    if not hasattr(store, "reactivate_influencer_pool_product_job_finalizer"):
        return
    store.reactivate_influencer_pool_product_job_finalizer(
        source_record_id=source_record_id,
        product_id=product_id,
        run_id=run_id,
    )


def _process_author_detail_job(
    *,
    author_job: Mapping[str, Any],
    product_id: str,
    record_id: str,
    source_images: Any,
    holiday_name: str,
    include_contact: bool,
    apply_mutations: bool,
    existing_state: Mapping[str, Any] | None,
    target_target: TableTarget,
    target_schema: Mapping[str, Any],
    fastmoss: FastMossHTTPSession,
    store: Phase1RuntimeStore,
    execution: SyntheticExecutionContext,
    settings: Mapping[str, Any],
    before_browser_wait: Callable[[], None],
    after_browser_wait: Callable[[], None],
) -> dict[str, Any]:
    influencer_id = str(author_job.get("influencer_id", "") or "").strip()
    uid = str(author_job.get("uid", "") or "").strip() or None
    list_follower_count = _coerce_number(author_job.get("follower_count"))
    sold_count = _coerce_number(author_job.get("sold_count"))
    force_author_refresh = bool(author_job.get("force_refresh")) or _coerce_bool(
        settings.get("force_author_detail_refresh"),
        default=False,
    )

    if not force_author_refresh and influencer_state_has_source_product(existing_state, product_id):
        return {
            "influencer_id": influencer_id,
            "action": "skipped_checkpoint",
            "merged_state": dict(existing_state or {}),
            "non_blocking_failures": [],
        }

    bundle = _fetch_author_bundle_with_recovery(
        fastmoss=fastmoss,
        settings=settings,
        product_id=product_id,
        uid=uid,
        unique_id=influencer_id,
        author_uid=str(uid or ""),
        include_contact=include_contact,
        before_browser_wait=before_browser_wait,
        after_browser_wait=after_browser_wait,
    )
    author_index = dict(bundle.get("author_index") or {})
    if list_follower_count > 0 and _coerce_number(
        author_index.get("follower_count")
        or author_index.get("fans_count")
    ) <= 0:
        author_index["follower_count"] = list_follower_count
        bundle = dict(bundle)
        bundle["author_index"] = author_index

    incoming_state = _build_incoming_influencer_state(
        influencer_id=influencer_id,
        product_id=product_id,
        sold_count=sold_count,
        holiday_name=holiday_name,
        source_images=source_images,
        bundle=bundle,
    )
    merged_state = merge_influencer_facts(existing_state, incoming_state)
    target_record_id = str(
        merged_state.get("target_record_id", "") or merged_state.get("record_id", "") or ""
    ).strip()

    action = ""
    non_blocking_failures: list[dict[str, Any]] = []
    if apply_mutations:
        influencer_non_blocking_failures: list[dict[str, Any]] = []
        writable_fields = build_influencer_write_fields(
            target_schema=target_schema,
            influencer_state=merged_state,
            client=target_target.client,
            parent_node=target_target.app_token,
            session=fastmoss.session,
            non_blocking_failures=influencer_non_blocking_failures,
        )
        non_blocking_failures.extend(dict(warning) for warning in influencer_non_blocking_failures)
        if target_record_id:
            target_target.client.update_record(
                target_target.app_token,
                target_target.table_id,
                target_record_id,
                writable_fields,
            )
            action = "updated"
        else:
            response = target_target.client.create_record(
                target_target.app_token,
                target_target.table_id,
                writable_fields,
            )
            action = "created"
            target_record_id = _extract_created_record_id(response)
            merged_state["record_id"] = target_record_id
            merged_state["target_record_id"] = target_record_id

    persisted = persist_influencer_entity_snapshot(
        store=store,
        execution=execution,
        influencer_state=merged_state,
        table_url=target_target.table_url,
        target_record_id=target_record_id,
        source_key=influencer_id,
    )
    if isinstance(persisted.get("entity_snapshot"), Mapping):
        merged_state["entity_snapshot"] = persisted.get("entity_snapshot")
    if isinstance(persisted.get("entity"), Mapping):
        merged_state["entity"] = persisted.get("entity")
    if target_record_id:
        merged_state["target_record_id"] = target_record_id
        merged_state["record_id"] = target_record_id

    return {
        "influencer_id": influencer_id,
        "action": action,
        "merged_state": merged_state,
        "non_blocking_failures": non_blocking_failures,
    }


def _build_sync_settings(params: Mapping[str, Any]) -> dict[str, Any]:
    table_url = str(params.get("table_url", "") or "").strip()
    target_table_url = str(params.get("target_table_url", "") or "").strip()
    if not table_url:
        raise ValueError("table_url is required")
    if not target_table_url:
        raise ValueError("target_table_url is required")

    run_mode = _normalize_run_mode(params.get("run_mode"))
    return {
        "table_url": table_url,
        "target_table_url": target_table_url,
        "access_token": _resolve_access_token(params),
        "fastmoss_phone": _resolve_secret_param(params, "fastmoss_phone", "fastmoss_phone_env"),
        "fastmoss_password": _resolve_secret_param(params, "fastmoss_password", "fastmoss_password_env"),
        "fastmoss_region": str(params.get("fastmoss_region") or "US").strip() or "US",
        "profile_ref": str(params.get("profile_ref", "") or "").strip(),
        "browser_provider_name": str(params.get("browser_provider_name", "") or "").strip(),
        "browser_profile_id": str(params.get("browser_profile_id", "") or "").strip(),
        "browser_workspace_id": str(params.get("browser_workspace_id", "") or "").strip(),
        "browser_risk_wait_timeout_seconds": _coerce_non_negative_float(
            params.get("browser_risk_wait_timeout_seconds"),
            default=DEFAULT_FASTMOSS_BROWSER_RISK_WAIT_TIMEOUT_SECONDS,
        ),
        "browser_risk_poll_interval_seconds": _coerce_non_negative_float(
            params.get("browser_risk_poll_interval_seconds"),
            default=DEFAULT_FASTMOSS_BROWSER_RISK_POLL_INTERVAL_SECONDS,
        ),
        "include_contact": _coerce_bool(params.get("include_contact"), default=False),
        "debug_cookie_timeline": _coerce_bool(params.get("debug_cookie_timeline"), default=True),
        "debug_timeline_max_events": max(
            _coerce_positive_int(params.get("debug_timeline_max_events")) or DEFAULT_DEBUG_TIMELINE_MAX_EVENTS,
            1,
        ),
        "max_source_rows": _coerce_positive_int(params.get("max_source_rows")),
        "max_author_pages": _coerce_positive_int(params.get("max_author_pages")),
        "max_author_detail_jobs_per_source_row": _coerce_positive_int(
            params.get("max_author_detail_jobs_per_source_row")
        )
        or DEFAULT_MAX_AUTHOR_DETAIL_JOBS_PER_SOURCE_ROW,
        "drain_author_detail_jobs_inline": _coerce_bool(
            params.get("drain_author_detail_jobs_inline"),
            default=True,
        ),
        "request_delay_min_seconds": _coerce_non_negative_float(
            params.get("request_delay_min_seconds"),
            default=1.0,
        ),
        "request_delay_max_seconds": _coerce_non_negative_float(
            params.get("request_delay_max_seconds"),
            default=3.0,
        ),
        "run_mode": run_mode,
        "queue_mode": str(params.get("queue_mode") or "inline").strip().lower() or "inline",
        "apply_mutations": run_mode in RUN_MODES_WITH_MUTATIONS,
    }


def _build_influencer_pool_worker_settings(params: Mapping[str, Any]) -> dict[str, Any]:
    raw_worker_kinds = str(params.get("worker_kinds") or "product,author,finalizer")
    worker_kinds = {
        item.strip().lower()
        for item in raw_worker_kinds.replace(";", ",").split(",")
        if item.strip()
    }
    allowed_worker_kinds = {"product", "author", "finalizer"}
    normalized_worker_kinds = sorted(worker_kinds & allowed_worker_kinds) or [
        "product",
        "author",
        "finalizer",
    ]
    raw_max_iterations = params.get("worker_max_iterations")
    if raw_max_iterations in (None, ""):
        max_iterations = DEFAULT_INFLUENCER_POOL_WORKER_MAX_ITERATIONS
    else:
        max_iterations = _coerce_positive_int(raw_max_iterations)
    return {
        "worker_kinds": normalized_worker_kinds,
        "max_iterations": max_iterations,
        "stop_when_idle": _coerce_bool(params.get("worker_stop_when_idle"), default=True),
        "max_idle_cycles": max(_coerce_positive_int(params.get("worker_max_idle_cycles")) or 1, 1),
        "poll_interval_seconds": _coerce_non_negative_float(
            params.get("worker_poll_interval_seconds"),
            default=1.0,
        ),
        "finalizer_scan_limit": max(_coerce_positive_int(params.get("finalizer_scan_limit")) or 20, 1),
    }


def _build_queue_settings(params: Mapping[str, Any]) -> dict[str, Any]:
    defaults = get_execution_control_defaults()
    configured_db_url = str(params.get("execution_control_db_url") or defaults.db_url).strip()
    configured_db_path = str(params.get("execution_control_db_path") or defaults.db_path).strip()
    if not configured_db_url and "://" in configured_db_path:
        configured_db_url = configured_db_path
        configured_db_path = str(defaults.db_path)
    return {
        "db_url": configured_db_url,
        "db_path": configured_db_path,
        "requested_by": str(
            params.get("execution_requested_by") or params.get("requested_by") or defaults.requested_by
        ).strip(),
        "worker_id": str(
            params.get("execution_worker_id") or params.get("worker_id") or defaults.worker_id
        ).strip(),
        "lease_seconds": max(
            _coerce_non_negative_float(params.get("execution_lease_seconds"), default=float(defaults.lease_seconds)),
            5.0,
        ),
        "heartbeat_interval_seconds": max(
            _coerce_non_negative_float(
                params.get("execution_heartbeat_interval_seconds"),
                default=float(defaults.heartbeat_interval_seconds),
            ),
            0.2,
        ),
        "poll_interval_seconds": max(
            _coerce_non_negative_float(
                params.get("execution_poll_interval_seconds"),
                default=float(defaults.poll_interval_seconds),
            ),
            0.05,
        ),
        "retry_delay_seconds": max(
            _coerce_non_negative_float(
                params.get("execution_retry_delay_seconds"),
                default=30.0,
            ),
            0.1,
        ),
        "wait_timeout_seconds": max(
            _coerce_non_negative_float(
                params.get("execution_wait_timeout_seconds"),
                default=float(defaults.wait_timeout_seconds),
            ),
            1.0,
        ),
        "resource_code": build_controlled_resource_code(dict(params)),
    }


def _build_table_target(table_url: str, access_token: str) -> TableTarget:
    table_meta = parse_table_url(table_url)
    return TableTarget(
        client=FeishuBitableClient(access_token),
        table_url=table_url,
        app_token=table_meta["app_token"],
        table_id=table_meta["table_id"],
        view_id=table_meta.get("view_id", ""),
    )


def _create_runtime_store(params: Mapping[str, Any]) -> Phase1RuntimeStore:
    defaults = get_execution_control_defaults()
    db_url = str(params.get("execution_control_db_url") or defaults.db_url).strip()
    db_path = str(params.get("execution_control_db_path") or defaults.db_path).strip()
    return Phase1RuntimeStore(db_url=db_url, db_path=db_path)


def _load_influencer_snapshot_records(
    store: Phase1RuntimeStore,
    target_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    seen_entity_ids: set[str] = set()
    for raw_record in target_records:
        fields = raw_record.get("fields")
        if not isinstance(fields, Mapping):
            continue
        influencer_id = str(fields.get(DEFAULT_INFLUENCER_ID_FIELD_NAME, "") or "").strip()
        if not influencer_id:
            continue
        entity = store.get_or_create_entity(
            entity_type="influencer",
            canonical_key=f"tiktok_influencer:{influencer_id}",
        )
        if entity.entity_id in seen_entity_ids:
            continue
        seen_entity_ids.add(entity.entity_id)
        snapshot = store.load_latest_entity_snapshot(entity_id=entity.entity_id)
        if snapshot is not None:
            snapshots.append(snapshot.to_dict())
    return snapshots


def _is_candidate_source_record(raw_record: Mapping[str, Any]) -> bool:
    fields = raw_record.get("fields")
    if not isinstance(fields, Mapping):
        return False
    product_status = _normalize_status_field_value(fields.get(DEFAULT_COMPETITOR_STATUS_FIELD_NAME))
    if product_status == UNAVAILABLE_PRODUCT_STATUS_VALUE:
        return False
    sync_status = _normalize_status_field_value(fields.get(DEFAULT_COMPETITOR_SYNC_STATUS_FIELD_NAME))
    return sync_status in SOURCE_STATUS_PENDING_VALUES


def _source_record_requests_full_refresh(
    *,
    source_status: str,
    product_id: str,
    influencer_index: Mapping[str, Mapping[str, Any]],
) -> bool:
    if str(source_status or "").strip():
        return False
    normalized_product_id = str(product_id or "").strip()
    if not normalized_product_id:
        return False
    return any(
        influencer_state_has_source_product(state, normalized_product_id)
        for state in influencer_index.values()
    )


def _build_influencer_failure_detail(
    *,
    exc: Exception,
    influencer_id: str,
    default_stage: str,
) -> dict[str, Any]:
    detail = {
        "influencer_id": str(influencer_id or "").strip(),
        "stage": str(default_stage or "").strip(),
        "field": "",
        "url": "",
        "error": str(exc),
        "error_type": "unexpected",
    }
    if isinstance(exc, InfluencerTraceError):
        trace_payload = exc.to_dict()
        detail.update(
            {
                "influencer_id": trace_payload.get("influencer_id") or detail["influencer_id"],
                "stage": trace_payload.get("stage") or detail["stage"],
                "field": trace_payload.get("field") or "",
                "url": trace_payload.get("url") or "",
                "error": trace_payload.get("error") or detail["error"],
                "error_type": trace_payload.get("cause_type") or "trace_error",
            }
        )
        return detail
    if isinstance(exc, FastMossHTTPError):
        detail.update(
            {
                "stage": str(exc.stage or detail["stage"]),
                "url": str(exc.path or ""),
                "error": _format_fastmoss_error(exc),
                "error_type": _classify_fastmoss_error_type(exc),
                "code": str(exc.response_code or ""),
                "status_code": exc.status_code,
                "method": str(exc.method or ""),
                "path": str(exc.path or ""),
                "fastmoss_error": exc.to_dict(),
            }
        )
    return detail


def _update_source_sync_status(
    *,
    source_target: TableTarget,
    record_id: str,
    status_value: str,
    apply_mutations: bool,
) -> None:
    if not apply_mutations or not record_id:
        return
    source_target.client.update_record(
        source_target.app_token,
        source_target.table_id,
        record_id,
        {DEFAULT_COMPETITOR_SYNC_STATUS_FIELD_NAME: status_value},
    )


def _resolve_access_token(params: Mapping[str, Any]) -> str:
    direct_token = str(params.get("access_token", "") or "").strip()
    if direct_token:
        return direct_token

    env_name = str(params.get("access_token_env", "") or "").strip()
    if not env_name:
        raise ValueError("access_token or access_token_env is required")
    env_value = str(os.getenv(env_name, "") or "").strip()
    if env_value:
        return env_value
    raise ValueError(f"Environment variable {env_name} is empty")


def _resolve_secret_param(params: Mapping[str, Any], direct_key: str, env_key: str) -> str:
    direct_value = str(params.get(direct_key, "") or "").strip()
    if direct_value:
        return direct_value
    env_name = str(params.get(env_key, "") or "").strip()
    if not env_name:
        raise ValueError(f"{direct_key} or {env_key} is required")
    env_value = str(os.getenv(env_name, "") or "").strip()
    if env_value:
        return env_value
    raise ValueError(f"Environment variable {env_name} is empty")


def _normalize_run_mode(value: Any) -> str:
    normalized = str(value or "draft").strip().lower()
    return normalized or "draft"


def _normalize_status_field_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("name", "text", "value"):
            normalized = _normalize_status_field_value(value.get(key))
            if normalized:
                return normalized
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            normalized = _normalize_status_field_value(item)
            if normalized:
                return normalized
        return ""
    return str(value).strip()


def _extract_single_name(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return str(value.get("name") or value.get("text") or value.get("value") or "").strip()
    if isinstance(value, (list, tuple)):
        for item in value:
            normalized = _extract_single_name(item)
            if normalized:
                return normalized
        return ""
    return str(value).strip() if value is not None else ""


def _extract_author_list_follower_count(author_row: Mapping[str, Any]) -> float:
    """Read follower count from the product-author list row before fetching details."""

    for source in _author_list_follower_sources(author_row):
        for key in AUTHOR_LIST_FOLLOWER_COUNT_KEYS:
            if key not in source:
                continue
            count = _coerce_number(source.get(key))
            if count > 0:
                return count
    return 0.0


def _author_list_follower_sources(author_row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    sources: list[Mapping[str, Any]] = [author_row]
    for key in ("author", "author_info", "user", "user_info", "account", "profile"):
        value = author_row.get(key)
        if isinstance(value, Mapping):
            sources.append(value)
    return sources


def _coerce_number(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text:
        return 0.0
    multiplier = 1.0
    lower_text = text.lower()
    suffix_multipliers = (
        ("亿", 100_000_000.0),
        ("万", 10_000.0),
        ("b", 1_000_000_000.0),
        ("m", 1_000_000.0),
        ("k", 1_000.0),
    )
    for suffix, suffix_multiplier in suffix_multipliers:
        if lower_text.endswith(suffix):
            multiplier = suffix_multiplier
            text = text[: -len(suffix)]
            break
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return 0.0
    try:
        return float(match.group(0)) * multiplier
    except ValueError:
        return 0.0


def _extract_created_record_id(payload: Mapping[str, Any]) -> str:
    data = payload.get("data")
    if isinstance(data, Mapping):
        record = data.get("record") or data.get("item") or {}
        if isinstance(record, Mapping):
            return str(record.get("record_id", "") or "").strip()
    return ""


def _summarize_status_counts(items: list[Mapping[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status", "") or "").strip() or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return {
        "total": len(items),
        "counts": counts,
    }


def _build_write_summary(
    items: list[Mapping[str, Any]],
    failed_items: list[Mapping[str, Any]],
) -> dict[str, Any]:
    created_author_count = sum(_coerce_count(item.get("created_author_count")) for item in items)
    updated_author_count = sum(_coerce_count(item.get("updated_author_count")) for item in items)
    already_synced_author_count = sum(
        _coerce_count(item.get("already_synced_author_count")) for item in items
    )
    synced_author_count = created_author_count + updated_author_count
    hard_stopped = any(_item_requests_hard_stop(item) for item in items)
    hard_stop_item = next((item for item in items if _item_requests_hard_stop(item)), {})
    return {
        "created_author_count": created_author_count,
        "updated_author_count": updated_author_count,
        "synced_author_count": synced_author_count,
        "already_synced_author_count": already_synced_author_count,
        "failed_item_count": len(failed_items),
        "hard_stopped": hard_stopped,
        "hard_stop_code": str(hard_stop_item.get("hard_stop_code", "") or ""),
        "hard_stop_reason": str(hard_stop_item.get("hard_stop_reason", "") or ""),
    }


def _create_influencer_pool_summary_outbox(
    *,
    params: Mapping[str, Any],
    store: Phase1RuntimeStore,
    execution: SyntheticExecutionContext,
    summary: Mapping[str, Any],
    write_summary: Mapping[str, Any],
    items: list[Mapping[str, Any]],
    failed_items: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    channel_code = str(params.get("notification_channel_code", "") or "").strip()
    reply_target = str(params.get("reply_target", "") or "").strip()
    if not channel_code or not reply_target:
        return []

    ref_id = str(execution.request_id or execution.run_id or execution.execution_id).strip()
    if not ref_id:
        return []

    message_text = _build_influencer_pool_outbox_text(
        summary=summary,
        write_summary=write_summary,
        failed_items=failed_items,
    )
    outbox = store.create_notification_outbox(
        channel_code=channel_code,
        event_type="sync_tk_influencer_pool.completed",
        ref_id=ref_id,
        reply_target=reply_target,
        payload={
            "message_text": message_text,
            "request_id": str(execution.request_id or ""),
            "run_id": str(execution.run_id or ""),
            "task_code": "sync_tk_influencer_pool",
            "summary": dict(summary),
            "write_summary": dict(write_summary),
            "failed_items": [dict(item) for item in failed_items],
            "items": [dict(item) for item in items],
        },
        dedupe_key=f"sync_tk_influencer_pool.summary:{execution.run_id}",
    )
    if hasattr(outbox, "to_dict"):
        return [outbox.to_dict()]
    if isinstance(outbox, Mapping):
        return [dict(outbox)]
    return []


def _build_influencer_pool_outbox_text(
    *,
    summary: Mapping[str, Any],
    write_summary: Mapping[str, Any],
    failed_items: list[Mapping[str, Any]],
) -> str:
    if bool(write_summary.get("hard_stopped")):
        result_label = "已中断，等待重试"
    elif _coerce_count(write_summary.get("failed_item_count")) > 0:
        result_label = "失败重试"
    else:
        result_label = "完成"

    lines = [
        f"达人池同步任务：{result_label}",
        f"来源记录总数：{_coerce_count(summary.get('total'))}",
        f"已新增达人：{_coerce_count(write_summary.get('created_author_count'))}",
        f"已更新达人：{_coerce_count(write_summary.get('updated_author_count'))}",
        f"已跳过已完成达人：{_coerce_count(write_summary.get('already_synced_author_count'))}",
        f"失败竞品条目：{_coerce_count(write_summary.get('failed_item_count'))}",
    ]

    failure = _extract_first_failure_for_notification(failed_items)
    if failure:
        lines.extend(
            [
                f"失败记录：{failure['record_id']}",
                f"SKU-ID：{failure['product_id']}",
                f"失败达人：{failure['influencer_id']}",
                f"失败原因：{failure['code']} / {failure['error_type']} / {failure['stage']} / {failure['path']}",
            ]
        )
    return "\n".join(lines)


def _extract_first_failure_for_notification(
    failed_items: list[Mapping[str, Any]],
) -> dict[str, str]:
    if not failed_items:
        return {}
    item = failed_items[0]
    failed_influencers = item.get("failed_influencers")
    failed_influencer = {}
    if isinstance(failed_influencers, list) and failed_influencers:
        first_failed = failed_influencers[0]
        if isinstance(first_failed, Mapping):
            failed_influencer = dict(first_failed)

    fastmoss_error = failed_influencer.get("fastmoss_error")
    if not isinstance(fastmoss_error, Mapping):
        fastmoss_error = {}

    return {
        "record_id": str(item.get("record_id", "") or ""),
        "product_id": str(item.get("product_id", "") or ""),
        "influencer_id": str(failed_influencer.get("influencer_id", "") or ""),
        "code": str(
            item.get("hard_stop_code")
            or failed_influencer.get("code")
            or fastmoss_error.get("response_code")
            or ""
        ),
        "error_type": str(failed_influencer.get("error_type") or item.get("error_type") or ""),
        "stage": str(failed_influencer.get("stage") or fastmoss_error.get("stage") or ""),
        "path": str(
            failed_influencer.get("path")
            or failed_influencer.get("url")
            or fastmoss_error.get("path")
            or ""
        ),
    }


def _item_requests_hard_stop(item: Mapping[str, Any]) -> bool:
    if bool(item.get("hard_stop")):
        return True
    return str(item.get("hard_stop_code", "") or "").strip() == FASTMOSS_HARD_STOP_RESPONSE_CODE


def _is_fastmoss_hard_stop_error(exc: FastMossHTTPError) -> bool:
    return str(exc.response_code or "").strip() == FASTMOSS_HARD_STOP_RESPONSE_CODE


def _coerce_count(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _coerce_positive_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 0
    return max(normalized, 0)


def _coerce_non_negative_float(value: Any, *, default: float) -> float:
    if value in (None, ""):
        return max(default, 0.0)
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return max(default, 0.0)
    return max(normalized, 0.0)


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _format_fastmoss_error(exc: FastMossHTTPError) -> str:
    parts = []
    if exc.stage:
        parts.append(f"stage={exc.stage}")
    if exc.method or exc.path:
        method = exc.method or "GET"
        path = exc.path or ""
        parts.append(f"request={method} {path}".strip())
    if exc.response_code not in (None, ""):
        parts.append(f"code={exc.response_code}")
    if exc.status_code not in (None, ""):
        parts.append(f"http={exc.status_code}")
    risk_control_hint = _detect_fastmoss_risk_control_hint(exc)
    if risk_control_hint:
        parts.append(f"hint={risk_control_hint}")
    message = str(exc.message or "FastMoss request failed").strip()
    if message:
        parts.append(f"message={message}")
    return " | ".join(parts) if parts else "FastMoss request failed"


def _classify_fastmoss_error_type(exc: FastMossHTTPError) -> str:
    code = str(exc.response_code or "").strip()
    message = str(exc.message or "").strip()
    if code.startswith("MSG_SAFE_"):
        return "fastmoss_risk_control"
    if isinstance(exc, FastMossHTTPError) and code in {"MAG_AUTH_3001", "MAG_AUTH_3002", "MAG_AUTH_3017", "MAG_AUTH_3019"}:
        return "fastmoss_permission"
    if "查看详情次数不足" in message or code == "MAG_AUTH_3002":
        return "fastmoss_detail_quota"
    if "升级会员" in message or code == "MAG_AUTH_3017":
        return "fastmoss_membership_required"
    if isinstance(exc, FastMossHTTPError) and exc.status_code in {429, 500, 502, 503, 504}:
        return "fastmoss_transient_http"
    if isinstance(exc, FastMossHTTPError) and (exc.method or exc.path):
        return "fastmoss_api_error"
    return "fastmoss_error"


def _detect_fastmoss_risk_control_hint(exc: FastMossHTTPError) -> str:
    code = str(exc.response_code or "").strip()
    path = str(exc.path or "").strip()
    if code == "MSG_SAFE_0001" and path.startswith("/api/goods/v3/"):
        return "likely_slider_captcha"
    if code == "MSG_SAFE_0001":
        return "likely_risk_control"
    return ""
