from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

from automation_business_scaffold.contracts.handler.api import BOUND_API_HANDLERS
from automation_business_scaffold.contracts.handler.contract import HandlerContext
from automation_business_scaffold.contracts.handler.shared import success_result
from automation_business_scaffold.control_plane.executor.runner import _sanitize_task_payload
from automation_business_scaffold.control_plane.executor.workflow_registry import (
    load_workflow_runtime,
)
from automation_business_scaffold.domains.tiktok.flows import (
    outreach_creator_video_metrics as metric_flow_module,
)
from automation_business_scaffold.domains.tiktok.flows import (
    outreach_product_videos as product_video_flow_module,
)
from automation_business_scaffold.domains.tiktok.flows.tiktok_influencer_outreach_sync.orchestrator import (
    _merge_video_rows,
    advance_stage,
    finalize_request,
    release_request_after_child_completion,
)
from automation_business_scaffold.domains.tiktok.flows.tiktok_influencer_outreach_sync.summary import (
    _build_summary,
)
from automation_business_scaffold.domains.tiktok.jobs.outreach_creator_video_metric_refresh import (
    outreach_creator_video_metric_refresh_handler,
)
from automation_business_scaffold.domains.tiktok.jobs.product_video_outreach_check import (
    product_video_outreach_check_handler,
)
from automation_business_scaffold.domains.tiktok.flows.outreach_product_videos import (
    canonical_tiktok_video_url,
    match_outreach_rows_to_videos,
    normalize_product_video_rows,
)
from automation_business_scaffold.infrastructure.fastmoss.http_session import (
    FastMossHTTPError,
    FastMossHTTPSession,
)
from automation_business_scaffold.domains.tiktok.mappers.feishu_outreach_source_mapper import (
    build_outreach_query_window,
    group_outreach_rows_by_product,
    outreach_source_adapter,
)
from automation_business_scaffold.domains.tiktok.projections.feishu_outreach_projection import (
    outreach_result_projection_mapper,
)
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition
from automation_business_scaffold.infrastructure.facts.tk_fact_store import TKFactStore


FLOW_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "automation_business_scaffold"
    / "domains"
    / "tiktok"
    / "flows"
    / "tiktok_influencer_outreach_sync"
)


def test_outreach_summary_owns_final_assembly() -> None:
    orchestrator_source = (FLOW_ROOT / "orchestrator.py").read_text(encoding="utf-8")
    summary_source = (FLOW_ROOT / "summary.py").read_text(encoding="utf-8")

    assert "def finalize_request" in summary_source
    assert "def _build_summary" in summary_source
    assert "create_notification_outbox" in summary_source
    assert "build_outbox_message_text" in summary_source
    assert "def _build_summary" not in orchestrator_source
    assert "create_notification_outbox" not in orchestrator_source


def test_outreach_source_adapter_reads_existing_fields_and_skip_summary() -> None:
    result = outreach_source_adapter(
        [
            {
                "record_id": "rec1",
                "fields": {
                    "SKUID": " 123 ",
                    "达人ID": " creator ",
                    "检查时间": "2026/05/20",
                    "播放量": "1,234",
                    "视频数量": 2,
                    "更新时间": "2026-05-21T09:00:00",
                },
            },
            {"record_id": "rec2", "fields": {"SKUID": "", "达人ID": "creator2"}},
            {"record_id": "rec3", "fields": {"SKUID": "123", "达人ID": ""}},
            {
                "record_id": "rec4",
                "fields": {
                    "SKUID": "123",
                    "达人ID": "creator4",
                    "视频链接": {"link": "https://example.test/v"},
                    "视频发布时间": "2026.05.18",
                    "检查时间": "2026-05-19",
                    "播放量": "5",
                    "视频数量": "1",
                    "更新时间": "2026/05/22",
                },
            },
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
            "existing_video_published_date": "",
            "existing_play_count": 1234,
            "existing_video_count": 2,
            "last_checked_at": "2026-05-20",
            "last_updated_at": "2026-05-21",
            "source_fields": {
                "SKUID": " 123 ",
                "达人ID": " creator ",
                "检查时间": "2026/05/20",
                "播放量": "1,234",
                "视频数量": 2,
                "更新时间": "2026-05-21T09:00:00",
            },
            "writeback_context": {
                "table_code": "tk_influencer_outreach",
                "target_table_ref": "tbl",
                "record_id": "rec1",
            },
            "source_context": {
                "source_record_id": "rec1",
                "source_table_ref": "tbl",
                "source_fields": {
                    "SKUID": " 123 ",
                    "达人ID": " creator ",
                    "检查时间": "2026/05/20",
                    "播放量": "1,234",
                    "视频数量": 2,
                    "更新时间": "2026-05-21T09:00:00",
                },
            },
        },
        {
            "source_record_id": "rec4",
            "business_key": "outreach:rec4",
            "product_id": "123",
            "creator_unique_id": "creator4",
            "existing_video_url": "https://example.test/v",
            "existing_video_published_date": "2026-05-18",
            "existing_play_count": 5,
            "existing_video_count": 1,
            "last_checked_at": "2026-05-19",
            "last_updated_at": "2026-05-22",
            "source_fields": {
                "SKUID": "123",
                "达人ID": "creator4",
                "视频链接": {"link": "https://example.test/v"},
                "视频发布时间": "2026.05.18",
                "检查时间": "2026-05-19",
                "播放量": "5",
                "视频数量": "1",
                "更新时间": "2026/05/22",
            },
            "writeback_context": {
                "table_code": "tk_influencer_outreach",
                "target_table_ref": "tbl",
                "record_id": "rec4",
            },
            "source_context": {
                "source_record_id": "rec4",
                "source_table_ref": "tbl",
                "source_fields": {
                    "SKUID": "123",
                    "达人ID": "creator4",
                    "视频链接": {"link": "https://example.test/v"},
                    "视频发布时间": "2026.05.18",
                    "检查时间": "2026-05-19",
                    "播放量": "5",
                    "视频数量": "1",
                    "更新时间": "2026/05/22",
                },
            },
        },
    ]
    assert result["adapter_summary"]["skip_reasons"] == {
        "missing_product_id": 1,
        "missing_creator_unique_id": 1,
    }


def test_outreach_source_adapter_reads_feishu_date_timestamps() -> None:
    result = outreach_source_adapter(
        [
            {
                "record_id": "rec-date",
                "fields": {
                    "SKUID": "1732266893752242590",
                    "达人ID": "creator",
                    "检查时间": 1780070400000,
                    "更新时间": "1780156800",
                },
            }
        ],
        {"source_table_ref": "tbl"},
    )

    row = result["source_rows"][0]
    assert row["last_checked_at"] == "2026-05-30"
    assert row["last_updated_at"] == "2026-05-31"

    groups = group_outreach_rows_by_product(
        result["source_rows"],
        trigger_date="2026-06-01",
    )
    assert groups[0]["query_window"] == {
        "mode": "date_range",
        "start_date": "2026-05-29",
        "end_date": "2026-06-01",
    }


def test_outreach_source_adapter_preserves_missing_play_count_as_empty() -> None:
    result = outreach_source_adapter(
        [
            {
                "record_id": "rec-empty-play",
                "fields": {
                    "SKUID": "1732266893752242590",
                    "达人ID": "shaycroft",
                    "视频链接": {
                        "link": "https://www.tiktok.com/@shaycroft/video/7642489799089179918"
                    },
                    "视频数量": 1,
                },
            }
        ],
        {"source_table_ref": "tbl"},
    )

    row = result["source_rows"][0]
    assert row["existing_play_count"] is None

    groups = group_outreach_rows_by_product(
        result["source_rows"],
        trigger_date="2026-06-01",
    )
    assert groups[0]["rows"][0]["existing_play_count"] is None


def test_outreach_query_window_honors_request_payload_priority() -> None:
    rows = [{"existing_video_url": "", "last_checked_at": "2026-05-19"}]

    assert build_outreach_query_window(
        rows,
        trigger_date="2026-05-22",
        request_payload={"force_full": True, "start_date": "2026-05-01", "end_date": "2026-05-22"},
    ) == {"mode": "d_type", "d_type": 0}
    assert build_outreach_query_window(
        rows,
        trigger_date="2026-05-22",
        request_payload={"start_date": "2026/05/10", "end_date": "2026/05/22"},
    ) == {"mode": "date_range", "start_date": "2026-05-10", "end_date": "2026-05-22"}


def test_outreach_query_window_uses_checked_dates_for_rows_without_video() -> None:
    assert build_outreach_query_window([{"last_checked_at": ""}], trigger_date="2026-05-22") == {
        "mode": "d_type",
        "d_type": 0,
    }
    assert build_outreach_query_window(
        [
            {
                "existing_video_url": "",
                "last_checked_at": "2026-05-21",
                "last_updated_at": "2026-05-27",
            },
            {
                "existing_video_url": "",
                "last_checked_at": "2026-05-19",
                "last_updated_at": "2026-05-28",
            },
            {
                "existing_video_url": "https://example.test/v",
                "last_checked_at": "2026-04-01",
                "last_updated_at": "2026-05-22",
            },
        ],
        trigger_date="2026-05-22",
    ) == {"mode": "date_range", "start_date": "2026-05-18", "end_date": "2026-05-22"}


def test_outreach_query_window_uses_latest_update_when_all_rows_have_video() -> None:
    assert build_outreach_query_window(
        [
            {"existing_video_url": "https://example.test/a", "last_updated_at": "2026-05-20"},
            {"existing_video_url": "https://example.test/b", "last_updated_at": "2026-05-22"},
        ],
        trigger_date="2026-05-28",
    ) == {"mode": "date_range", "start_date": "2026-05-21", "end_date": "2026-05-28"}
    assert build_outreach_query_window(
        [{"existing_video_url": "https://example.test/a", "last_updated_at": ""}],
        trigger_date="2026-05-28",
    ) == {"mode": "d_type", "d_type": 0}


def test_group_outreach_rows_by_product_preserves_window_context() -> None:
    groups = group_outreach_rows_by_product(
        [
            {
                "source_record_id": "rec1",
                "product_id": "p1",
                "creator_unique_id": "creator",
                "existing_video_url": "",
                "existing_video_published_date": "",
                "existing_play_count": 12,
                "existing_video_count": 2,
                "last_checked_at": "2026-05-20",
                "last_updated_at": "2026-05-21",
                "source_fields": {"SKUID": "p1"},
                "source_context": {
                    "source_record_id": "rec1",
                    "source_table_ref": "tbl",
                    "source_fields": {"SKUID": "p1"},
                    "request_payload": {"start_date": "2026-05-01", "end_date": "2026-05-28"},
                },
                "writeback_context": {"record_id": "rec1"},
            }
        ],
        trigger_date="2026-05-28",
    )

    assert groups == [
        {
            "product_id": "p1",
            "trigger_date": "2026-05-28",
            "query_window": {
                "mode": "date_range",
                "start_date": "2026-05-01",
                "end_date": "2026-05-28",
            },
            "rows": [
                {
                    "source_record_id": "rec1",
                    "creator_unique_id": "creator",
                    "existing_video_url": "",
                    "existing_video_published_date": "",
                    "existing_play_count": 12,
                    "existing_video_count": 2,
                    "last_checked_at": "2026-05-20",
                    "last_updated_at": "2026-05-21",
                    "source_fields": {"SKUID": "p1"},
                    "source_context": {
                        "source_record_id": "rec1",
                        "source_table_ref": "tbl",
                        "source_fields": {"SKUID": "p1"},
                        "request_payload": {"start_date": "2026-05-01", "end_date": "2026-05-28"},
                    },
                    "writeback_context": {"record_id": "rec1"},
                }
            ],
        }
    ]


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
    assert "fm-sign" not in captured["headers"]
    assert captured["headers"]["sec-ch-ua"] == (
        '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"'
    )
    assert captured["headers"]["sec-ch-ua-mobile"] == "?0"
    assert captured["headers"]["sec-ch-ua-platform"] == '"macOS"'
    assert captured["data"] is None


def test_fastmoss_video_overview_http_request_matches_roxy_browser_headers(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        def json(self) -> dict[str, object]:
            return {"code": 200, "data": {}}

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
        time_factory=lambda: 1781322123,
        nonce_factory=lambda: "27195563",
    )
    monkeypatch.setattr(session.session, "request", fake_request)

    session.get_video_overview("7631305086458662158")

    assert captured["method"] == "GET"
    assert captured["params"] == {
        "id": "7631305086458662158",
        "_time": 1781322123,
        "cnonce": "27195563",
    }
    assert captured["data"] is None
    assert (
        captured["headers"]["Referer"]
        == "https://www.fastmoss.com/zh/media-source/video/7631305086458662158"
    )
    assert captured["headers"]["region"] == "Global"
    assert captured["headers"]["fm-sign"] == "8ef64cd1b3da4b126d4a9fd216c237c4"
    assert captured["headers"]["sec-ch-ua"] == (
        '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"'
    )
    assert captured["headers"]["sec-ch-ua-mobile"] == "?0"
    assert captured["headers"]["sec-ch-ua-platform"] == '"macOS"'


def test_outreach_submit_payload_injects_default_fastmoss_env_refs() -> None:
    payload = _sanitize_task_payload(
        {"control_action": "submit", "trigger_date": "2026-05-22"},
        task_code="tiktok_influencer_outreach_sync",
    )

    assert payload["fastmoss_live_fetch"] is True
    assert payload["fastmoss_phone_env"] == "FASTMOSS_PHONE"
    assert payload["fastmoss_password_env"] == "FASTMOSS_PASSWORD"


def test_product_video_check_uses_api_worker_mock_rows_without_live_fastmoss_config(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    result = product_video_outreach_check_handler(
        HandlerContext(
            request_id="req",
            job_id="job",
            handler_code="product_video_outreach_check",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            payload={
                "product_id": "p1",
                "rows": [{"source_record_id": "rec1", "creator_unique_id": "creator"}],
                "mock_fastmoss_product_videos": [
                    {"product_id": "p1", "unique_id": "creator", "video_id": "1"}
                ],
            },
        )
    )

    assert result.status == "success"
    assert result.summary["matched_row_count"] == 1


def test_product_video_check_uses_fixed_page_size_five(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}

    class FakeSession:
        def __enter__(self):  # noqa: ANN001
            return self

        def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
            return False

        def list_product_videos(self, product_id, *, page, pagesize, **kwargs):  # noqa: ANN001
            captured.update(
                {"product_id": product_id, "page": page, "pagesize": pagesize, "kwargs": kwargs}
            )
            return {"code": 200, "data": {"list": []}}

    monkeypatch.setattr(
        product_video_flow_module, "build_fastmoss_session", lambda *args, **kwargs: FakeSession()
    )
    monkeypatch.setattr(
        product_video_flow_module, "prepare_fastmoss_session", lambda *args, **kwargs: {}
    )

    result = product_video_outreach_check_handler(
        HandlerContext(
            request_id="req",
            job_id="job",
            handler_code="product_video_outreach_check",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            payload={
                "product_id": "p1",
                "fastmoss_live_fetch": True,
                "fastmoss_video_page_size": 30,
                "rows": [],
            },
        )
    )

    assert result.status == "success"
    assert captured["pagesize"] == 5


def test_product_video_check_retries_business_500_at_page_level(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    sleeps: list[float] = []
    calls: list[int] = []

    class FakeSession:
        def __enter__(self):  # noqa: ANN001
            return self

        def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
            return False

        def list_product_videos(self, product_id, *, page, pagesize, **kwargs):  # noqa: ANN001
            del product_id, pagesize, kwargs
            calls.append(page)
            if page == 1:
                return {
                    "code": 200,
                    "data": {
                        "total": 10,
                        "list": [
                            {"product_id": "p1", "unique_id": "creator", "video_id": str(index)}
                            for index in range(1, 6)
                        ],
                    },
                }
            if page == 2 and calls.count(2) == 1:
                raise FastMossHTTPError(
                    "Internal Server Error",
                    status_code=200,
                    response_code=500,
                    payload={"code": 500, "msg": "Internal Server Error"},
                    path="/api/goods/v3/video",
                    params={"page": 2},
                )
            return {
                "code": 200,
                "data": {
                    "total": 10,
                    "list": [
                        {"product_id": "p1", "unique_id": "creator", "video_id": str(index)}
                        for index in range(6, 11)
                    ],
                },
            }

    monkeypatch.setattr(
        product_video_flow_module, "build_fastmoss_session", lambda *args, **kwargs: FakeSession()
    )
    monkeypatch.setattr(
        product_video_flow_module, "prepare_fastmoss_session", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        product_video_flow_module.time, "sleep", lambda seconds: sleeps.append(seconds)
    )

    result = product_video_outreach_check_handler(
        HandlerContext(
            request_id="req",
            job_id="job",
            handler_code="product_video_outreach_check",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            payload={
                "product_id": "p1",
                "fastmoss_live_fetch": True,
                "rows": [{"source_record_id": "rec1", "creator_unique_id": "creator"}],
            },
        )
    )

    assert result.status == "success"
    assert result.summary["fetched_video_count"] == 10
    assert calls == [1, 2, 2]
    assert sleeps == [10.0]


def test_product_video_check_records_failed_page_after_page_retry_exhausted(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    sleeps: list[float] = []

    class FakeSession:
        def __enter__(self):  # noqa: ANN001
            return self

        def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
            return False

        def list_product_videos(self, product_id, *, page, pagesize, **kwargs):  # noqa: ANN001
            del product_id, pagesize, kwargs
            if page == 1:
                return {
                    "code": 200,
                    "data": {
                        "total": 10,
                        "list": [
                            {"product_id": "p1", "unique_id": "creator", "video_id": str(index)}
                            for index in range(1, 6)
                        ],
                    },
                }
            raise FastMossHTTPError(
                "Internal Server Error",
                status_code=200,
                response_code=500,
                payload={"code": 500, "msg": "Internal Server Error"},
                path="/api/goods/v3/video",
                params={"page": page},
            )

    monkeypatch.setattr(
        product_video_flow_module, "build_fastmoss_session", lambda *args, **kwargs: FakeSession()
    )
    monkeypatch.setattr(
        product_video_flow_module, "prepare_fastmoss_session", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        product_video_flow_module.time, "sleep", lambda seconds: sleeps.append(seconds)
    )

    result = product_video_outreach_check_handler(
        HandlerContext(
            request_id="req",
            job_id="job",
            handler_code="product_video_outreach_check",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            payload={
                "product_id": "p1",
                "fastmoss_live_fetch": True,
                "rows": [{"source_record_id": "rec1", "creator_unique_id": "creator"}],
            },
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.retryable is True
    assert result.result["failed_page"] == 2
    assert [row["video_id"] for row in result.result["partial_video_rows"]] == [
        "1",
        "2",
        "3",
        "4",
        "5",
    ]
    assert sleeps == [10.0, 20.0, 30.0]


def test_product_video_check_persists_full_video_audit_for_success(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    result = product_video_outreach_check_handler(
        HandlerContext(
            request_id="req",
            job_id="job",
            handler_code="product_video_outreach_check",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            payload={
                "product_id": "p1",
                "trigger_date": "2026-05-22",
                "rows": [{"source_record_id": "rec1", "creator_unique_id": "creator"}],
                "mock_fastmoss_product_videos": [
                    {
                        "product_id": "p1",
                        "unique_id": "creator",
                        "video_id": "1",
                        "create_date": "2026-05-20",
                    },
                    {
                        "product_id": "p1",
                        "author": {"unique_id": "other"},
                        "video_id": "2",
                        "create_date": "2026-05-21",
                    },
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


def test_product_video_check_indexes_product_videos_in_fact_db(
    monkeypatch, tmp_path, runtime_db_url
) -> None:
    monkeypatch.chdir(tmp_path)
    result = product_video_outreach_check_handler(
        HandlerContext(
            request_id="req",
            job_id="job",
            handler_code="product_video_outreach_check",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            payload={
                "product_id": "p1",
                "fact_db_url": runtime_db_url,
                "rows": [{"source_record_id": "rec1", "creator_unique_id": "creator"}],
                "mock_fastmoss_product_videos": [
                    {
                        "product_id": "p1",
                        "unique_id": "creator",
                        "video_id": "1",
                        "create_date": "2026-05-20",
                    },
                    {
                        "product_id": "p1",
                        "unique_id": "creator",
                        "video_id": "2",
                        "create_date": "2026-05-21",
                    },
                ],
            },
        )
    )

    fact_store = TKFactStore(db_url=runtime_db_url)
    videos = fact_store.list_videos_by_product_and_creator(
        product_id="p1", creator_unique_id="creator"
    )

    assert result.status == "success"
    assert result.result["indexed_video_count"] == 2
    assert result.result["new_video_count"] == 2
    assert [video["video_id"] for video in videos] == ["1", "2"]
    assert videos[0]["published_date"] == "2026-05-20"


def test_creator_video_metric_refresh_persists_snapshots_and_writes_aggregate(
    monkeypatch, runtime_db_url
) -> None:
    fact_store = TKFactStore(db_url=runtime_db_url)
    creator_key = fact_store.build_creator_key(unique_id="creator")
    for video_id, published_date in (("1", "2026-05-20"), ("2", "2026-05-19")):
        video = fact_store.upsert_video(
            video_id=video_id,
            creator_key=creator_key,
            creator_unique_id="creator",
            product_id="p1",
            video_url=canonical_tiktok_video_url("creator", video_id),
            source_platform="fastmoss",
            facts={"published_date": published_date},
        )
        fact_store.upsert_video_product_relation(
            video_key=video["video_key"], product_id="p1", source_platform="fastmoss"
        )

    captured: dict[str, object] = {}

    def fake_write(context: HandlerContext):  # noqa: ANN001
        captured["payload"] = context.payload
        return success_result(
            context,
            summary={"written_count": 1, "skipped_count": 0, "failed_count": 0},
            result={"written_count": 1, "records": [{"status": "success"}]},
        )

    monkeypatch.setattr(metric_flow_module, "feishu_table_write_handler", fake_write)

    result = outreach_creator_video_metric_refresh_handler(
        HandlerContext(
            request_id="req",
            job_id="job",
            handler_code="outreach_creator_video_metric_refresh",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            payload={
                "product_id": "p1",
                "creator_unique_id": "creator",
                "source_record_id": "rec1",
                "trigger_date": "2026-05-28",
                "target_table_ref": "tbl",
                "fact_db_url": runtime_db_url,
                "source_fields": {
                    "视频链接": "",
                    "视频发布时间": "",
                    "检查时间": "",
                    "播放量": 0,
                    "视频数量": 0,
                },
                "mock_fastmoss_video_overviews": {
                    "1": {"video_id": "1", "play_count": 10},
                    "2": {"video_id": "2", "play_count": 30},
                },
            },
        )
    )

    write_payload = captured["payload"]
    assert isinstance(write_payload, dict)
    records = write_payload["records"]
    assert isinstance(records, list)
    fields = records[0]["fields"]

    assert result.status == "success"
    assert result.result["video_count"] == 2
    assert result.result["total_play_count"] == 40
    assert result.result["highest_play_video_url"] == "https://www.tiktok.com/@creator/video/2"
    assert result.result["highest_play_count"] == 30
    assert result.result["earliest_published_date"] == "2026-05-19"
    assert fields == {
        "视频链接": {
            "link": "https://www.tiktok.com/@creator/video/2",
            "text": "https://www.tiktok.com/@creator/video/2",
        },
        "播放量": "<1W",
        "视频数量": 2,
        "视频发布时间": "2026-05-19",
        "更新时间": "2026-05-28",
    }


def test_creator_video_metric_refresh_writes_less_than_1w_when_existing_play_count_blank(
    monkeypatch, runtime_db_url
) -> None:
    fact_store = TKFactStore(db_url=runtime_db_url)
    creator_key = fact_store.build_creator_key(unique_id="shaycroft")
    video_url = canonical_tiktok_video_url("shaycroft", "7642489799089179918")
    video = fact_store.upsert_video(
        video_id="7642489799089179918",
        creator_key=creator_key,
        creator_unique_id="shaycroft",
        product_id="1732266893752242590",
        video_url=video_url,
        source_platform="fastmoss",
        facts={"published_date": "2026-05-22"},
    )
    fact_store.upsert_video_product_relation(
        video_key=video["video_key"],
        product_id="1732266893752242590",
        source_platform="fastmoss",
    )
    captured: dict[str, object] = {}

    def fake_write(context: HandlerContext):  # noqa: ANN001
        captured["payload"] = context.payload
        return success_result(
            context,
            summary={"written_count": 1, "skipped_count": 0, "failed_count": 0},
            result={"written_count": 1, "records": [{"status": "success"}]},
        )

    monkeypatch.setattr(metric_flow_module, "feishu_table_write_handler", fake_write)

    result = outreach_creator_video_metric_refresh_handler(
        HandlerContext(
            request_id="req",
            job_id="job",
            handler_code="outreach_creator_video_metric_refresh",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            payload={
                "product_id": "1732266893752242590",
                "creator_unique_id": "shaycroft",
                "source_record_id": "recvj0hfWqPJMS",
                "trigger_date": "2026-05-31",
                "target_table_ref": "tbl",
                "existing_play_count": None,
                "fact_db_url": runtime_db_url,
                "source_fields": {
                    "视频链接": {"link": video_url, "text": video_url},
                    "视频发布时间": "2026-05-22",
                    "视频数量": 1,
                },
                "mock_fastmoss_video_overviews": {
                    "7642489799089179918": {
                        "video_id": "7642489799089179918",
                        "play_count": 5866,
                    }
                },
            },
        )
    )

    write_payload = captured["payload"]
    assert isinstance(write_payload, dict)
    assert write_payload["records"][0]["fields"] == {
        "播放量": "<1W",
        "更新时间": "2026-05-31",
    }
    assert result.status == "success"
    assert result.result["feishu_written"] is True
    assert result.result["written_fields"] == ["播放量", "更新时间"]


def test_creator_video_metric_refresh_overview_failure_writes_no_partial_feishu(
    monkeypatch, runtime_db_url
) -> None:
    fact_store = TKFactStore(db_url=runtime_db_url)
    creator_key = fact_store.build_creator_key(unique_id="creator")
    video = fact_store.upsert_video(
        video_id="1",
        creator_key=creator_key,
        creator_unique_id="creator",
        product_id="p1",
        video_url=canonical_tiktok_video_url("creator", "1"),
        source_platform="fastmoss",
        facts={"published_date": "2026-05-20"},
    )
    fact_store.upsert_video_product_relation(
        video_key=video["video_key"], product_id="p1", source_platform="fastmoss"
    )
    calls: list[dict[str, object]] = []

    def fake_write(context: HandlerContext):  # noqa: ANN001
        calls.append(context.payload)
        return success_result(context, summary={"written_count": 1}, result={"written_count": 1})

    monkeypatch.setattr(metric_flow_module, "feishu_table_write_handler", fake_write)

    result = outreach_creator_video_metric_refresh_handler(
        HandlerContext(
            request_id="req",
            job_id="job",
            handler_code="outreach_creator_video_metric_refresh",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            payload={
                "product_id": "p1",
                "creator_unique_id": "creator",
                "source_record_id": "rec1",
                "trigger_date": "2026-05-28",
                "writeback_enabled": True,
                "fact_db_url": runtime_db_url,
                "mock_fastmoss_video_overviews": {},
            },
        )
    )

    assert result.status == "failed"
    assert calls == []


def test_creator_video_metric_refresh_writes_check_time_when_no_video_for_empty_link(
    monkeypatch, runtime_db_url
) -> None:
    captured: dict[str, object] = {}

    def fake_write(context: HandlerContext):  # noqa: ANN001
        captured["payload"] = context.payload
        return success_result(
            context,
            summary={"written_count": 1, "skipped_count": 0, "failed_count": 0},
            result={"written_count": 1, "records": [{"status": "success"}]},
        )

    monkeypatch.setattr(metric_flow_module, "feishu_table_write_handler", fake_write)

    result = outreach_creator_video_metric_refresh_handler(
        HandlerContext(
            request_id="req",
            job_id="job",
            handler_code="outreach_creator_video_metric_refresh",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            payload={
                "product_id": "p1",
                "creator_unique_id": "creator",
                "source_record_id": "rec1",
                "trigger_date": "2026-05-28",
                "target_table_ref": "tbl",
                "writeback_enabled": True,
                "fact_db_url": runtime_db_url,
                "existing_video_url": "",
                "last_checked_at": "",
            },
        )
    )

    write_payload = captured["payload"]
    assert isinstance(write_payload, dict)
    assert write_payload["records"][0]["fields"] == {"检查时间": "2026-05-28"}
    assert result.status == "success"
    assert result.result["video_count"] == 0
    assert result.result["written_fields"] == ["检查时间"]


def test_creator_video_metric_refresh_skips_existing_link_from_source_fields_when_index_missing(
    monkeypatch, runtime_db_url
) -> None:
    calls: list[dict[str, object]] = []

    def fake_write(context: HandlerContext):  # noqa: ANN001
        calls.append(context.payload)
        return success_result(context, summary={"written_count": 1}, result={"written_count": 1})

    monkeypatch.setattr(metric_flow_module, "feishu_table_write_handler", fake_write)

    result = outreach_creator_video_metric_refresh_handler(
        HandlerContext(
            request_id="req",
            job_id="job",
            handler_code="outreach_creator_video_metric_refresh",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            payload={
                "product_id": "p1",
                "creator_unique_id": "creator",
                "source_record_id": "rec1",
                "trigger_date": "2026-05-28",
                "target_table_ref": "tbl",
                "fact_db_url": runtime_db_url,
                "source_fields": {
                    "视频链接": {"link": "https://www.tiktok.com/@creator/video/existing"}
                },
            },
        )
    )

    assert result.status == "skipped"
    assert result.result["skip_reason"] == "existing_link_missing_from_index"
    assert calls == []


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


class _OutreachFallbackStore:
    def __init__(self, *, current_stage: str, jobs: list[dict[str, object]]) -> None:
        self.request = SimpleNamespace(
            request_id="req-fallback",
            task_code="tiktok_influencer_outreach_sync",
            status="waiting",
            current_stage=current_stage,
            progress_stage=current_stage,
            payload={},
        )
        self.jobs = jobs
        self.executions: list[SimpleNamespace] = []
        self.enqueued_executions: list[dict[str, object]] = []
        self.requeued_jobs: list[dict[str, object]] = []
        self.failed_jobs: list[dict[str, object]] = []
        self.request_updates: list[dict[str, object]] = []

    def load_task_request(self, request_id):  # noqa: ANN001
        assert request_id == self.request.request_id
        return self.request

    def update_task_request(self, **kwargs):  # noqa: ANN001
        self.request_updates.append(dict(kwargs))
        for key, value in kwargs.items():
            setattr(self.request, key, value)
        return self.request

    def list_api_worker_jobs_for_request(self, request_id, job_code=None):  # noqa: ANN001
        assert request_id == self.request.request_id
        return [job for job in self.jobs if not job_code or job.get("job_code") == job_code]

    def list_task_executions(self, request_id):  # noqa: ANN001
        assert request_id == self.request.request_id
        return list(self.executions)

    def enqueue_task_executions(self, **kwargs):  # noqa: ANN001
        self.enqueued_executions.extend(kwargs["items"])
        return {"created_count": len(kwargs["items"])}

    def requeue_waiting_api_worker_job(self, **kwargs):  # noqa: ANN001
        self.requeued_jobs.append(dict(kwargs))
        for job in self.jobs:
            if job.get("job_id") == kwargs["job_id"]:
                job["status"] = "pending"
                job["payload"] = kwargs["payload"]
                return job
        return dict(kwargs)

    def mark_waiting_api_worker_job_failed(self, **kwargs):  # noqa: ANN001
        self.failed_jobs.append(dict(kwargs))
        for job in self.jobs:
            if job.get("job_id") == kwargs["job_id"]:
                job["status"] = "finished"
                job["result_status"] = "failed"
                job["error_code"] = kwargs["error_code"]
                return job
        return dict(kwargs)


def _outreach_fallback_job(
    job_id: str, *, stage_code: str, status: str = "waiting"
) -> dict[str, object]:
    return {
        "job_id": job_id,
        "job_code": "outreach_creator_video_metric_refresh"
        if stage_code == "refresh_creator_video_metrics_and_writeback"
        else "product_video_outreach_check",
        "business_key": job_id,
        "status": status,
        "payload": {"stage_code": stage_code},
        "result": {
            "handler_result": {
                "status": "fallback_required",
                "result": {
                    "fallback_required": True,
                    "fallback_reason": "fastmoss_api_security_verification",
                    "security_context": {"path": "/api/video/overview"},
                    "verification_request": {
                        "path": "/api/video/overview",
                        "params": {"video_id": job_id},
                    },
                },
            }
        },
    }


def _browser_execution(
    *, execution_id: str, status: str, payload: dict[str, object], result: dict[str, object]
) -> SimpleNamespace:
    execution = SimpleNamespace(
        execution_id=execution_id, status=status, payload=payload, result=result
    )
    execution.to_dict = lambda: {
        "execution_id": execution_id,
        "status": status,
        "payload": payload,
        "result": result,
    }
    return execution


def test_outreach_release_routes_waiting_fallback_before_pending_jobs() -> None:
    store = _OutreachFallbackStore(
        current_stage="refresh_creator_video_metrics_and_writeback",
        jobs=[
            _outreach_fallback_job(
                "job-waiting", stage_code="refresh_creator_video_metrics_and_writeback"
            ),
            _outreach_fallback_job(
                "job-pending",
                stage_code="refresh_creator_video_metrics_and_writeback",
                status="pending",
            ),
        ],
    )

    released = release_request_after_child_completion(store, request_id=store.request.request_id)

    assert released == [
        {
            "request_id": "req-fallback",
            "stage_code": "fastmoss_security_browser_fallback",
            "released": True,
        }
    ]
    assert store.request_updates[-1]["current_stage"] == "fastmoss_security_browser_fallback"


def test_outreach_refresh_stage_routes_fallback_before_waiting_on_pending_jobs() -> None:
    store = _OutreachFallbackStore(
        current_stage="refresh_creator_video_metrics_and_writeback",
        jobs=[
            _outreach_fallback_job(
                "job-waiting", stage_code="refresh_creator_video_metrics_and_writeback"
            ),
            _outreach_fallback_job(
                "job-pending",
                stage_code="refresh_creator_video_metrics_and_writeback",
                status="pending",
            ),
        ],
    )

    result = advance_stage(
        store=store,
        request=store.request,
        workflow=get_workflow_definition("tiktok_influencer_outreach_sync"),
        stage_code="refresh_creator_video_metrics_and_writeback",
    )

    assert result["action"] == "advance"
    assert result["next_stage"] == "fastmoss_security_browser_fallback"


def test_outreach_release_waits_while_browser_fallback_execution_is_active() -> None:
    store = _OutreachFallbackStore(
        current_stage="fastmoss_security_browser_fallback",
        jobs=[],
    )
    store.executions.append(
        _browser_execution(
            execution_id="browser-1",
            status="running",
            payload={
                "stage_code": "fastmoss_security_browser_fallback",
                "fallback_digest": "digest",
            },
            result={},
        )
    )

    released = release_request_after_child_completion(store, request_id=store.request.request_id)

    assert released == []
    assert store.request_updates == []


def test_outreach_browser_fallback_success_requeues_all_waiting_jobs() -> None:
    store = _OutreachFallbackStore(
        current_stage="fastmoss_security_browser_fallback",
        jobs=[
            _outreach_fallback_job(
                "job-1", stage_code="refresh_creator_video_metrics_and_writeback"
            ),
            _outreach_fallback_job(
                "job-2", stage_code="refresh_creator_video_metrics_and_writeback"
            ),
        ],
    )

    dispatch = advance_stage(
        store=store,
        request=store.request,
        workflow=get_workflow_definition("tiktok_influencer_outreach_sync"),
        stage_code="fastmoss_security_browser_fallback",
    )
    assert dispatch["action"] == "waiting"
    fallback_payload = store.enqueued_executions[0]["payload"]
    assert fallback_payload["source_job_ids"] == ["job-1", "job-2"]
    store.executions.append(
        _browser_execution(
            execution_id="browser-1",
            status="success",
            payload={
                "stage_code": "fastmoss_security_browser_fallback",
                "fallback_digest": fallback_payload["fallback_digest"],
            },
            result={"handler_result": {"status": "success", "result": {"resolved": True}}},
        )
    )

    result = advance_stage(
        store=store,
        request=store.request,
        workflow=get_workflow_definition("tiktok_influencer_outreach_sync"),
        stage_code="fastmoss_security_browser_fallback",
    )

    assert result["action"] == "waiting"
    assert result["current_stage"] == "refresh_creator_video_metrics_and_writeback"
    assert [job["job_id"] for job in store.requeued_jobs] == ["job-1", "job-2"]
    assert all(job["payload"]["browser_fallback_resolved"] is True for job in store.requeued_jobs)


def test_outreach_browser_fallback_failure_fails_waiting_jobs_and_parent() -> None:
    store = _OutreachFallbackStore(
        current_stage="fastmoss_security_browser_fallback",
        jobs=[
            _outreach_fallback_job("job-1", stage_code="index_product_videos"),
            _outreach_fallback_job("job-2", stage_code="index_product_videos"),
        ],
    )
    dispatch = advance_stage(
        store=store,
        request=store.request,
        workflow=get_workflow_definition("tiktok_influencer_outreach_sync"),
        stage_code="fastmoss_security_browser_fallback",
    )
    fallback_payload = store.enqueued_executions[0]["payload"]
    store.executions.append(
        _browser_execution(
            execution_id="browser-1",
            status="failed",
            payload={
                "stage_code": "fastmoss_security_browser_fallback",
                "fallback_digest": fallback_payload["fallback_digest"],
            },
            result={
                "handler_result": {
                    "status": "failed",
                    "error": {
                        "error_type": "auth_failure",
                        "error_code": "fastmoss_auth_session_recovery_required",
                        "message": "FastMoss auth recovery is still required after browser fallback.",
                    },
                }
            },
        )
    )

    result = advance_stage(
        store=store,
        request=store.request,
        workflow=get_workflow_definition("tiktok_influencer_outreach_sync"),
        stage_code="fastmoss_security_browser_fallback",
    )

    assert dispatch["action"] == "waiting"
    assert result["action"] == "finalize"
    assert result["final_status"] == "failed"
    assert result["error_code"] == "fastmoss_security_browser_fallback_failed"
    assert result["failed_waiting_job_count"] == 2
    assert [job["job_id"] for job in store.failed_jobs] == ["job-1", "job-2"]
    assert all(
        job["error_code"] == "fastmoss_security_browser_fallback_failed"
        for job in store.failed_jobs
    )


def test_product_video_matching_normalizes_unique_id_and_selects_earliest_video() -> None:
    videos = normalize_product_video_rows(
        [
            {
                "product_id": "p1",
                "author": {"unique_id": "creator"},
                "video_id": "2",
                "create_date": "2026-05-20",
            },
            {
                "product_id": "p1",
                "unique_id": "creator",
                "video_id": "1",
                "create_date": "2026-05-19",
            },
            {
                "product_id": "other",
                "unique_id": "creator",
                "video_id": "0",
                "create_date": "2026-05-18",
            },
        ]
    )

    result = match_outreach_rows_to_videos(
        product_id="p1",
        rows=[
            {"source_record_id": "rec1", "creator_unique_id": "creator"},
            {"source_record_id": "rec2", "creator_unique_id": "missing"},
        ],
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


def test_outreach_projection_writes_metric_fields_and_can_overwrite_highest_video_url() -> None:
    matched = outreach_result_projection_mapper(
        {
            "source_record_id": "rec1",
            "creator_unique_id": "creator",
            "highest_play_video_url": canonical_tiktok_video_url("creator", "123"),
            "earliest_published_date": "2026-05-20",
            "total_play_count": 42,
            "video_count": 2,
            "checked_at": "2026-05-22",
        },
        {
            "workflow_code": "tiktok_influencer_outreach_sync",
            "stage_code": "refresh_creator_video_metrics_and_writeback",
        },
    )
    assert matched["op"] == "update"
    assert matched["record_id"] == "rec1"
    assert matched["fields"] == {
        "视频链接": {
            "link": "https://www.tiktok.com/@creator/video/123",
            "text": "https://www.tiktok.com/@creator/video/123",
        },
        "视频发布时间": "2026-05-20",
        "播放量": "<1W",
        "视频数量": 2,
        "更新时间": "2026-05-22",
    }

    refreshed = outreach_result_projection_mapper(
        {
            "source_record_id": "rec1",
            "highest_play_video_url": "https://www.tiktok.com/@creator/video/123",
            "earliest_published_date": "2026-05-20",
            "total_play_count": 120345,
            "video_count": 2,
            "checked_at": "2026-05-22",
            "existing_video_url": "https://www.tiktok.com/@creator/video/existing",
            "existing_video_published_date": "2026-05-18",
            "existing_play_count": 40000,
            "existing_video_count": 1,
        },
        {},
    )
    assert refreshed["fields"] == {
        "视频链接": {
            "link": "https://www.tiktok.com/@creator/video/123",
            "text": "https://www.tiktok.com/@creator/video/123",
        },
        "播放量": "12W",
        "视频数量": 2,
        "更新时间": "2026-05-22",
    }

    blank_play_count = outreach_result_projection_mapper(
        {
            "source_record_id": "rec1",
            "highest_play_video_url": "https://www.tiktok.com/@creator/video/123",
            "earliest_published_date": "2026-05-20",
            "total_play_count": 5866,
            "video_count": 1,
            "checked_at": "2026-05-22",
            "existing_video_url": "https://www.tiktok.com/@creator/video/123",
            "existing_video_published_date": "2026-05-20",
            "existing_play_count": None,
            "existing_video_count": 1,
            "source_fields": {"播放量": ""},
        },
        {},
    )
    assert blank_play_count["fields"] == {
        "播放量": "<1W",
        "更新时间": "2026-05-22",
    }


def test_outreach_projection_writes_only_check_time_when_no_aggregate_video_url() -> None:
    record = outreach_result_projection_mapper(
        {
            "source_record_id": "rec1",
            "creator_unique_id": "creator",
            "checked_at": "2026-05-22",
            "total_play_count": 0,
            "video_count": 0,
        },
        {},
    )

    assert record["fields"] == {"检查时间": "2026-05-22"}


def test_outreach_workflow_and_handler_are_registered() -> None:
    workflow = get_workflow_definition("tiktok_influencer_outreach_sync")
    assert workflow.entry_stage_code == "read_outreach_rows"
    assert [stage.stage_code for stage in workflow.stages] == [
        "read_outreach_rows",
        "index_product_videos",
        "fastmoss_security_browser_fallback",
        "refresh_creator_video_metrics_and_writeback",
        "ready_for_summary",
    ]
    job = workflow.require_job("product_video_outreach_check")
    assert job.worker_type == "api_worker"
    assert job.runtime_table == "api_worker_job"
    metric_job = workflow.require_job("outreach_creator_video_metric_refresh")
    assert metric_job.worker_type == "api_worker"
    assert "product_video_outreach_check" in BOUND_API_HANDLERS
    assert "outreach_creator_video_metric_refresh" in BOUND_API_HANDLERS
    assert load_workflow_runtime("tiktok_influencer_outreach_sync") is not None


def test_outreach_finalize_persists_request_and_outbox() -> None:
    class Store:
        def __init__(self) -> None:
            self.update_payload = {}
            self.outbox_payload = {}

        def update_task_request(self, **kwargs):  # noqa: ANN001
            self.update_payload = dict(kwargs)
            return SimpleNamespace(
                request_id=kwargs["request_id"],
                status=kwargs["status"],
                result_status="",
                current_stage=kwargs["current_stage"],
                summary=kwargs["summary"],
                result=kwargs["result"],
                to_dict=lambda: {"request_id": kwargs["request_id"], "status": kwargs["status"]},
            )

        def create_notification_outbox(self, **kwargs):  # noqa: ANN001
            self.outbox_payload = dict(kwargs)
            return SimpleNamespace(to_dict=lambda: {"outbox_id": "outbox1", "status": "pending"})

    store = Store()
    request = SimpleNamespace(
        request_id="req1",
        task_code="tiktok_influencer_outreach_sync",
        source_channel_code="feishu_bot_api",
        reply_target="user:1",
        payload={},
    )

    result = finalize_request(
        store=store,
        request=request,
        workflow=get_workflow_definition("tiktok_influencer_outreach_sync"),
        force_result={"final_status": "success", "matched_row_count": 0},
    )

    assert result["request_status"] == "success"
    assert store.update_payload["status"] == "success"
    assert store.update_payload["current_stage"] == "ready_for_summary"
    assert store.update_payload["worker_id"] == ""
    assert store.outbox_payload["event_type"] == "task_request.completed"
    assert store.outbox_payload["dedupe_key"] == "task_request.completed:req1"


def test_outreach_finalize_builds_detailed_outbox_message() -> None:
    class Store:
        def __init__(self) -> None:
            self.outbox_payload = {}
            self.jobs = [
                {
                    "job_code": "feishu_table_read",
                    "status": "success",
                    "payload": {"stage_code": "read_outreach_rows"},
                    "result": {
                        "adapter_summary": {
                            "input_row_count": 2,
                            "source_row_count": 2,
                            "skipped_count": 0,
                            "skip_reasons": {},
                        },
                    },
                },
                {
                    "job_code": "product_video_outreach_check",
                    "status": "success",
                    "payload": {"stage_code": "index_product_videos", "product_id": "sku1"},
                    "summary": {
                        "product_id": "sku1",
                        "fetch_status": "success",
                        "fetched_video_count": 2,
                        "matched_row_count": 1,
                        "indexed_video_count": 2,
                        "new_video_count": 0,
                        "updated_video_count": 2,
                    },
                    "result": {
                        "product_id": "sku1",
                        "fetch_status": "success",
                        "indexed_video_count": 2,
                        "new_video_count": 0,
                        "updated_video_count": 2,
                    },
                },
                {
                    "job_code": "outreach_creator_video_metric_refresh",
                    "status": "success",
                    "payload": {
                        "stage_code": "refresh_creator_video_metrics_and_writeback",
                        "product_id": "sku1",
                    },
                    "result": {
                        "product_id": "sku1",
                        "creator_unique_id": "creator1",
                        "source_record_id": "rec1",
                        "refresh_status": "success",
                        "video_count": 2,
                        "total_play_count": 12080,
                        "highest_play_video_url": "https://www.tiktok.com/@creator1/video/1",
                        "highest_play_count": 6051,
                        "earliest_published_date": "2026-04-24",
                        "feishu_written": True,
                        "written_fields": ["视频链接", "更新时间"],
                    },
                },
                {
                    "job_code": "outreach_creator_video_metric_refresh",
                    "status": "success",
                    "payload": {
                        "stage_code": "refresh_creator_video_metrics_and_writeback",
                        "product_id": "sku1",
                    },
                    "result": {
                        "product_id": "sku1",
                        "creator_unique_id": "creator2",
                        "source_record_id": "rec2",
                        "refresh_status": "success",
                        "video_count": 0,
                        "total_play_count": 0,
                        "feishu_written": True,
                        "written_fields": ["检查时间"],
                    },
                },
            ]

        def list_api_worker_jobs_for_request(self, request_id, job_code=None):  # noqa: ANN001
            del request_id
            return [job for job in self.jobs if not job_code or job.get("job_code") == job_code]

        def update_task_request(self, **kwargs):  # noqa: ANN001
            return SimpleNamespace(
                request_id=kwargs["request_id"],
                status=kwargs["status"],
                result_status=kwargs["status"],
                current_stage=kwargs["current_stage"],
                summary=kwargs["summary"],
                result=kwargs["result"],
                to_dict=lambda: {"request_id": kwargs["request_id"], "status": kwargs["status"]},
            )

        def create_notification_outbox(self, **kwargs):  # noqa: ANN001
            self.outbox_payload = dict(kwargs)
            return SimpleNamespace(to_dict=lambda: {"outbox_id": "outbox1", "status": "pending"})

    store = Store()
    result = finalize_request(
        store=store,
        request=SimpleNamespace(
            request_id="req-detail",
            task_code="tiktok_influencer_outreach_sync",
            source_channel_code="feishu_bot_api",
            reply_target="user:1",
            payload={},
            started_at=1780232645,
        ),
        workflow=get_workflow_definition("tiktok_influencer_outreach_sync"),
    )

    message_text = store.outbox_payload["payload"]["message_text"]
    assert result["request_status"] == "success"
    assert "TK达人建联表检查完成" in message_text
    assert "SKU：sku1" in message_text
    assert "达人ID：creator1" in message_text
    assert "更新字段：视频链接、更新时间" in message_text
    assert "最高播放视频播放量：6,051" in message_text
    assert "- 1 行未发现可写视频链接，仅写入检查时间" in message_text


def test_outreach_read_failure_finalizes_parent_as_failed() -> None:
    class Store:
        def list_api_worker_jobs_for_request(self, request_id, job_code=None):  # noqa: ANN001
            del request_id, job_code
            return [
                {
                    "job_id": "job-read",
                    "job_code": "feishu_table_read",
                    "status": "finished",
                    "result_status": "failed",
                    "error_code": "handler_unhandled_exception",
                    "error_text": "'outreach_creator_video_metric_refresh'",
                    "payload": {"stage_code": "read_outreach_rows"},
                    "result": {
                        "handler_result": {
                            "status": "failed",
                            "error": {"error_code": "handler_unhandled_exception"},
                        }
                    },
                }
            ]

    result = advance_stage(
        store=Store(),
        request=SimpleNamespace(request_id="req1", payload={}),
        workflow=get_workflow_definition("tiktok_influencer_outreach_sync"),
        stage_code="read_outreach_rows",
    )

    assert result["action"] == "finalize"
    assert result["final_status"] == "failed"
    assert result["failed_stage"] == "read_outreach_rows"
    assert result["failed_job_code"] == "feishu_table_read"
    assert result["failed_job_id"] == "job-read"


def test_outreach_summary_uses_row_level_metric_refresh_counts() -> None:
    class Store:
        def list_api_worker_jobs_for_request(self, request_id, job_code=None):  # noqa: ANN001
            del request_id
            jobs = [
                {
                    "status": "success",
                    "payload": {"stage_code": "read_outreach_rows"},
                    "result": {
                        "source_rows": [{"source_record_id": "rec1"}],
                        "adapter_summary": {
                            "input_row_count": 2,
                            "source_row_count": 1,
                            "skipped_count": 1,
                            "skip_reasons": {"missing_product_id": 1},
                        },
                    },
                },
                {
                    "status": "success",
                    "payload": {"stage_code": "index_product_videos"},
                    "result": {
                        "product_id": "p1",
                        "fetch_status": "success",
                        "indexed_video_count": 2,
                        "new_video_count": 1,
                        "updated_video_count": 1,
                    },
                },
                {
                    "status": "success",
                    "payload": {"stage_code": "refresh_creator_video_metrics_and_writeback"},
                    "result": {
                        "refresh_status": "success",
                        "video_count": 2,
                        "total_play_count": 40,
                        "feishu_written": True,
                        "written_fields": ["视频链接", "播放量", "视频数量"],
                    },
                },
                {
                    "status": "skipped",
                    "payload": {"stage_code": "refresh_creator_video_metrics_and_writeback"},
                    "result": {
                        "refresh_status": "skipped",
                        "skip_reason": "existing_link_missing_from_index",
                    },
                },
                {
                    "status": "failed",
                    "payload": {"stage_code": "refresh_creator_video_metrics_and_writeback"},
                    "result": {"refresh_status": "failed", "error_stage": "video_overview"},
                },
            ]
            return [
                job
                for job in jobs
                if not job_code
                or job_code
                in {
                    "feishu_table_read",
                    "product_video_outreach_check",
                    "outreach_creator_video_metric_refresh",
                }
            ]

    summary = _build_summary(
        store=Store(),
        request=SimpleNamespace(request_id="req", payload={}),
    )

    assert summary["final_status"] == "partial_success"
    assert summary["indexed_video_count"] == 2
    assert summary["creator_refresh_success_count"] == 1
    assert summary["index_missing_skipped_count"] == 1
    assert summary["overview_failed_count"] == 1
    assert summary["video_count_change_count"] == 1
    assert summary["play_count_change_count"] == 1
    assert summary["highest_video_change_count"] == 1
