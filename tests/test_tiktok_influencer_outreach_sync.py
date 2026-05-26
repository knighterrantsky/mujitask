from __future__ import annotations

import csv
import json
from pathlib import Path

from automation_business_scaffold.contracts.handler.browser import BOUND_BROWSER_HANDLERS
from automation_business_scaffold.contracts.handler.contract import HandlerContext
from automation_business_scaffold.control_plane.executor.runner import _sanitize_task_payload
from automation_business_scaffold.control_plane.executor.workflow_registry import load_workflow_runtime
from automation_business_scaffold.domains.tiktok.flows.tiktok_influencer_outreach_sync.orchestrator import _merge_video_rows
from automation_business_scaffold.capabilities.browser.fastmoss_product_video_outreach_handler import (
    fastmoss_product_video_outreach_handler,
)
from automation_business_scaffold.domains.tiktok.flows.outreach_product_videos import (
    canonical_tiktok_video_url,
    match_outreach_rows_to_videos,
    normalize_product_video_rows,
)
from automation_business_scaffold.infrastructure.fastmoss.http_session import FastMossHTTPSession
from automation_business_scaffold.domains.tiktok.mappers.feishu_outreach_source_mapper import (
    build_outreach_query_window,
    outreach_source_adapter,
)
from automation_business_scaffold.domains.tiktok.projections.feishu_outreach_projection import (
    outreach_result_projection_mapper,
)
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition


def test_outreach_source_adapter_normalizes_candidates_and_skip_summary() -> None:
    result = outreach_source_adapter(
        [
            {"record_id": "rec1", "fields": {"SKUID": " 123 ", "达人ID": " creator ", "检查时间": "2026/05/20"}},
            {"record_id": "rec2", "fields": {"SKUID": "", "达人ID": "creator2"}},
            {"record_id": "rec3", "fields": {"SKUID": "123", "达人ID": ""}},
            {"record_id": "rec4", "fields": {"SKUID": "123", "达人ID": "creator", "视频链接": {"link": "https://example.test/v"}}},
        ],
        {"source_table_ref": "tbl"},
    )

    assert result["source_rows"] == [
        {
            "source_record_id": "rec1",
            "business_key": "outreach:rec1",
            "product_id": "123",
            "creator_unique_id": "creator",
            "existing_video_url": "",
            "last_checked_at": "2026-05-20",
            "writeback_context": {"table_code": "tk_influencer_outreach", "target_table_ref": "tbl", "record_id": "rec1"},
            "source_context": {
                "source_record_id": "rec1",
                "source_table_ref": "tbl",
                "source_fields": {"SKUID": " 123 ", "达人ID": " creator ", "检查时间": "2026/05/20"},
            },
        },
        {
            "source_record_id": "rec2",
            "business_key": "outreach:rec2",
            "product_id": "123",
            "creator_unique_id": "creator2",
            "existing_video_url": "",
            "last_checked_at": "",
            "writeback_context": {"table_code": "tk_influencer_outreach", "target_table_ref": "tbl", "record_id": "rec2"},
            "source_context": {
                "source_record_id": "rec2",
                "source_table_ref": "tbl",
                "source_fields": {"SKUID": "", "达人ID": "creator2"},
            },
        },
    ]
    assert result["adapter_summary"]["skip_reasons"] == {
        "missing_product_id": 0,
        "missing_creator_unique_id": 1,
        "already_has_video_url": 1,
    }


def test_outreach_query_window_uses_full_or_incremental_window() -> None:
    assert build_outreach_query_window([{"last_checked_at": ""}], trigger_date="2026-05-22") == {"mode": "d_type", "d_type": 0}
    assert build_outreach_query_window(
        [{"last_checked_at": "2026-05-21"}, {"last_checked_at": "2026-05-19"}],
        trigger_date="2026-05-22",
    ) == {"mode": "date_range", "start_date": "2026-05-18", "end_date": "2026-05-22"}


def test_fastmoss_product_video_http_request_matches_browser_pagination(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        def json(self) -> dict[str, object]:
            return {"code": 200, "data": {"list": []}}

    def fake_request(method, url, *, params, data, headers, timeout):  # noqa: ANN001
        captured.update(
            {
                "method": method,
                "url": url,
                "params": dict(params),
                "data": data,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        return Response()

    session = FastMossHTTPSession(
        request_delay_range=(0.0, 0.0),
        time_factory=lambda: 1779704271,
        nonce_factory=lambda: "54361571",
    )
    monkeypatch.setattr(session.session, "request", fake_request)

    session.list_product_videos("1732266893752242590", page=2)

    assert captured["method"] == "GET"
    assert captured["params"] == {
        "page": 2,
        "product_id": "1732266893752242590",
        "order": "6,2",
        "pagesize": 5,
        "is_promoted": -1,
        "date_type": 28,
        "d_type": 0,
        "_time": 1779704271,
        "cnonce": "54361571",
    }
    assert "fm-sign" not in captured["params"]
    assert captured["data"] is None


def test_outreach_submit_payload_injects_default_fastmoss_env_refs() -> None:
    payload = _sanitize_task_payload({"control_action": "submit", "trigger_date": "2026-05-22"}, task_code="tiktok_influencer_outreach_sync")

    assert payload["fastmoss_live_fetch"] is True
    assert payload["fastmoss_phone_env"] == "FASTMOSS_PHONE"
    assert payload["fastmoss_password_env"] == "FASTMOSS_PASSWORD"


def test_product_video_check_uses_browser_rows_without_fastmoss_api_config(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    result = fastmoss_product_video_outreach_handler(
        HandlerContext(
            request_id="req",
            job_id="job",
            handler_code="product_video_outreach_check",
            worker_type="browser_worker",
            runtime_table="task_execution",
            payload={
                "product_id": "p1",
                "rows": [{"source_record_id": "rec1", "creator_unique_id": "creator"}],
                "mock_browser_video_pages": [{"rows": [{"product_id": "p1", "unique_id": "creator", "video_id": "1"}]}],
            },
        )
    )

    assert result.status == "success"
    assert result.result["collection_path"] == "browser"
    assert result.summary["matched_row_count"] == 1


def test_product_video_check_persists_full_video_audit_for_success(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    result = fastmoss_product_video_outreach_handler(
        HandlerContext(
            request_id="req",
            job_id="job",
            handler_code="product_video_outreach_check",
            worker_type="browser_worker",
            runtime_table="task_execution",
            payload={
                "product_id": "p1",
                "trigger_date": "2026-05-22",
                "rows": [{"source_record_id": "rec1", "creator_unique_id": "creator"}],
                "mock_browser_video_pages": [
                    {
                        "rows": [
                            {"product_id": "p1", "unique_id": "creator", "video_id": "1", "create_date": "2026-05-20"},
                            {"product_id": "p1", "author": {"unique_id": "other"}, "video_id": "2", "create_date": "2026-05-21"},
                        ]
                    }
                ],
            },
        )
    )

    audit = result.result["video_audit"]
    assert result.summary["fetched_video_count"] == 2
    assert result.summary["video_audit_ref"] == audit["json_path"]
    assert result.summary["unique_creator_count"] == 2
    assert audit["creator_ids"] == ["creator", "other"]
    assert json.loads(Path(audit["json_path"]).read_text(encoding="utf-8")) == [
        {
            "product_id": "p1",
            "creator_unique_id": "creator",
            "video_id": "1",
            "published_date": "2026-05-20",
            "video_url": "https://www.tiktok.com/@creator/video/1",
        },
        {
            "product_id": "p1",
            "creator_unique_id": "other",
            "video_id": "2",
            "published_date": "2026-05-21",
            "video_url": "https://www.tiktok.com/@other/video/2",
        },
    ]
    with Path(audit["csv_path"]).open(encoding="utf-8") as file:
        assert len(list(csv.DictReader(file))) == 2


def test_outreach_fallback_merges_carried_video_rows_across_retries() -> None:
    rows = _merge_video_rows(
        [
            {"product_id": "p1", "video_id": "1", "unique_id": "creator1"},
            {"product_id": "p1", "video_id": "2", "unique_id": "creator2"},
        ],
        [
            {"product_id": "p1", "video_id": "2", "unique_id": "creator2"},
            {"product_id": "p1", "video_id": "3", "unique_id": "creator3"},
        ],
    )

    assert rows == [
        {"product_id": "p1", "video_id": "1", "unique_id": "creator1"},
        {"product_id": "p1", "video_id": "2", "unique_id": "creator2"},
        {"product_id": "p1", "video_id": "3", "unique_id": "creator3"},
    ]


def test_product_video_matching_normalizes_unique_id_and_selects_earliest_video() -> None:
    videos = normalize_product_video_rows(
        [
            {"product_id": "p1", "author": {"unique_id": "creator"}, "video_id": "2", "create_date": "2026-05-20"},
            {"product_id": "p1", "unique_id": "creator", "video_id": "1", "create_date": "2026-05-19"},
            {"product_id": "other", "unique_id": "creator", "video_id": "0", "create_date": "2026-05-18"},
        ]
    )

    result = match_outreach_rows_to_videos(
        product_id="p1",
        rows=[{"source_record_id": "rec1", "creator_unique_id": "creator"}, {"source_record_id": "rec2", "creator_unique_id": "missing"}],
        videos=videos,
        query_window={"mode": "d_type", "d_type": 90},
        trigger_date="2026-05-22",
    )

    assert result["matched_rows"][0]["video_id"] == "1"
    assert result["matched_rows"][0]["video_url"] == "https://www.tiktok.com/@creator/video/1"
    assert result["unmatched_rows"] == [
        {
            "source_record_id": "rec2",
            "product_id": "p1",
            "creator_unique_id": "missing",
            "checked_at": "2026-05-22",
            "match_status": "unmatched",
            "writeback_context": {},
        }
    ]


def test_outreach_projection_writes_matched_fields_and_never_overwrites_existing_video_url() -> None:
    matched = outreach_result_projection_mapper(
        {
            "source_record_id": "rec1",
            "creator_unique_id": "creator",
            "video_url": canonical_tiktok_video_url("creator", "123"),
            "published_date": "2026-05-20",
            "checked_at": "2026-05-22",
            "match_status": "matched",
        },
        {"workflow_code": "tiktok_influencer_outreach_sync", "stage_code": "writeback_outreach_rows"},
    )
    assert matched["op"] == "update"
    assert matched["record_id"] == "rec1"
    assert matched["fields"] == {
        "视频链接": {"link": "https://www.tiktok.com/@creator/video/123", "text": "https://www.tiktok.com/@creator/video/123"},
        "视频发布时间": "2026-05-20",
        "检查时间": "2026-05-22",
    }

    stale = outreach_result_projection_mapper(
        {
            "source_record_id": "rec1",
            "video_url": "https://www.tiktok.com/@creator/video/123",
            "published_date": "2026-05-20",
            "checked_at": "2026-05-22",
            "existing_video_url": "https://www.tiktok.com/@creator/video/existing",
            "match_status": "matched",
        },
        {},
    )
    assert stale["fields"] == {"检查时间": "2026-05-22"}


def test_outreach_workflow_and_handler_are_registered() -> None:
    workflow = get_workflow_definition("tiktok_influencer_outreach_sync")
    assert workflow.entry_stage_code == "read_outreach_rows"
    assert "product_video_outreach_check" in BOUND_BROWSER_HANDLERS
    assert load_workflow_runtime("tiktok_influencer_outreach_sync") is not None
