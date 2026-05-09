from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from automation_business_scaffold.control_plane.executor.request_aggregation import (
    aggregate_request_children,
)
from automation_business_scaffold.control_plane.executor.worker_dispatch import (
    persist_api_worker_outcome,
)
from automation_business_scaffold.domains.tiktok.flows.search_keyword_selection_products.stages.selection_row_browser_fallback import (
    _selection_row_browser_fallback_candidates,
)
from automation_business_scaffold.infrastructure.runtime.request_lifecycle import (
    RuntimeRequestLifecycle,
)
from automation_business_scaffold.infrastructure.runtime.watchdog_recovery import (
    WatchdogRecoveryCoordinator,
)


class _FallbackAggregateStore:
    def list_api_worker_jobs_for_request(self, *, request_id: str) -> list[dict[str, Any]]:
        assert request_id == "req-1"
        return [
            {
                "job_id": "job-fallback",
                "status": "success",
                "payload": {"stage_code": "refresh_selection_rows"},
                "result": {
                    "handler_result": {
                        "status": "fallback_required",
                        "result": {"fallback_required": True},
                    }
                },
            }
        ]

    def list_task_executions(self, *, request_id: str) -> list[Any]:
        assert request_id == "req-1"
        return []


def test_fallback_required_transport_success_is_not_business_terminal_bucket() -> None:
    counts = aggregate_request_children(_FallbackAggregateStore(), request_id="req-1")

    assert counts["terminal_count"] == 1
    assert counts["fallback_required_count"] == 1
    assert counts["counts"]["fallback_required"] == 1
    assert counts["success_count"] == 0
    assert counts["failed_count"] == 0
    assert counts["skipped_count"] == 0


def test_api_worker_fallback_required_keeps_transport_and_workflow_semantics_separate() -> None:
    marked_calls: list[dict[str, Any]] = []

    class FakeStore:
        def mark_api_worker_job_waiting(self, **kwargs: Any) -> dict[str, Any]:
            marked_calls.append(dict(kwargs))
            return {"status": "waiting", "result_status": "", "stage": kwargs["stage"]}

    outcome = SimpleNamespace(
        should_mark_failed=False,
        worker_result=SimpleNamespace(status="fallback_required"),
        error=None,
        storage_summary=lambda: {"handler_status": "fallback_required"},
        storage_result=lambda: {
            "handler_result": {"status": "fallback_required", "result": {"fallback_required": True}}
        },
    )

    marked_job, success_count, failed_count = persist_api_worker_outcome(
        store=FakeStore(),
        job_id="job-1",
        run_id="run-1",
        outcome=outcome,
        retry_delay_seconds=1.0,
    )

    assert marked_job["status"] == "waiting"
    assert marked_job["result_status"] == ""
    assert marked_job["stage"] == "browser_fallback_required"
    assert success_count == 0
    assert failed_count == 0
    assert marked_calls[0]["result"]["handler_result"]["status"] == "fallback_required"


class _FakeResult:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def mappings(self) -> "_FakeResult":
        return self

    def first(self) -> dict[str, Any] | None:
        return self._row


class _RecordingConnection:
    def __init__(self, rows: list[dict[str, Any] | None]) -> None:
        self.rows = list(rows)
        self.statements: list[str] = []

    def execute(self, statement: Any, _params: dict[str, Any] | None = None) -> _FakeResult:
        self.statements.append(str(statement))
        row = self.rows.pop(0) if self.rows else None
        return _FakeResult(row)


class _FakeEngine:
    def __init__(self, connection: _RecordingConnection) -> None:
        self.connection = connection

    def begin(self) -> "_FakeEngine":
        return self

    def connect(self) -> "_FakeEngine":
        return self

    def __enter__(self) -> _RecordingConnection:
        return self.connection

    def __exit__(self, *_args: Any) -> None:
        return None


class _LifecycleStore:
    def __init__(self, connection: _RecordingConnection) -> None:
        self._engine = _FakeEngine(connection)

    @staticmethod
    def _text(sql: str) -> str:
        return sql

    @staticmethod
    def _request_from_row(row: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(**row)


def test_request_lifecycle_repair_does_not_promote_fallback_required_to_summary() -> None:
    connection = _RecordingConnection(
        [
            {
                "request_id": "req-1",
                "status": "waiting",
                "current_stage": "refresh_selection_rows",
            },
            {
                "total_count": 0,
                "terminal_count": 0,
                "success_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "fallback_required_count": 0,
                "active_count": 0,
            },
            {
                "total_count": 1,
                "terminal_count": 1,
                "success_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "fallback_required_count": 1,
                "active_count": 0,
            },
            None,
            {
                "request_id": "req-1",
                "status": "waiting",
                "current_stage": "refresh_selection_rows",
            },
        ]
    )

    result = RuntimeRequestLifecycle(_LifecycleStore(connection)).reconcile_waiting_children(
        request_id="req-1"
    )

    assert result["transitioned"] is False
    assert result["fallback_required_count"] == 1
    assert result["request"].status == "waiting"
    assert "SET status = 'ready_for_summary'" not in "\n".join(connection.statements)


def test_watchdog_waiting_scan_leaves_fallback_required_for_workflow_stage() -> None:
    class FakeStore:
        def __init__(self) -> None:
            self._engine = _FakeEngine(_RecordingConnection([]))
            self.payloads: list[dict[str, Any]] = []

        def _scan_runtime_rows(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return [{"request_id": "req-1", "status": "waiting"}]

        @staticmethod
        def _request_from_row(row: dict[str, Any]) -> SimpleNamespace:
            return SimpleNamespace(request_id=row["request_id"], status=row["status"], to_dict=lambda: row)

        @staticmethod
        def _aggregate_runtime_request_children(
            _connection: Any, *, request_id: str
        ) -> dict[str, int]:
            assert request_id == "req-1"
            return {
                "total_count": 1,
                "terminal_count": 1,
                "success_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "fallback_required_count": 1,
                "active_count": 0,
            }

        def _watchdog_payload(self, **kwargs: Any) -> dict[str, Any]:
            self.payloads.append(dict(kwargs))
            return dict(kwargs)

    store = FakeStore()

    candidates = WatchdogRecoveryCoordinator(store).scan_waiting_children_reconciliation(now=0)

    assert candidates == []
    assert store.payloads == []


def test_after_browser_row_job_prevents_repeated_selection_row_fallback_dispatch() -> None:
    class FakeStore:
        def list_api_worker_jobs_for_request(self, *, request_id: str) -> list[dict[str, Any]]:
            assert request_id == "req-1"
            return [
                {
                    "job_id": "row-fallback",
                    "job_code": "selection_row_refresh",
                    "business_key": "row-1",
                    "status": "success",
                    "payload": {
                        "stage_code": "refresh_selection_rows",
                        "source_record_id": "row-1",
                        "business_key": "row-1",
                    },
                    "result": {
                        "handler_result": {
                            "status": "fallback_required",
                            "result": {
                                "fallback_required": True,
                                "fallback_handler": "tiktok_product_browser_fetch",
                                "source_record_id": "row-1",
                                "business_entity_key": "row-1",
                                "browser_fallback_payload": {
                                    "normalized_product_url": "https://www.tiktok.com/shop/pdp/1"
                                },
                            },
                        }
                    },
                },
                {
                    "job_id": "row-after-browser",
                    "job_code": "selection_row_refresh",
                    "business_key": "row-1",
                    "status": "success",
                    "payload": {
                        "stage_code": "refresh_selection_rows",
                        "source_record_id": "row-1",
                        "browser_fallback_resolved": True,
                    },
                    "result": {"handler_result": {"status": "success", "result": {}}},
                },
            ]

        def list_task_executions(self, *, request_id: str) -> list[Any]:
            assert request_id == "req-1"
            return []

    candidates = _selection_row_browser_fallback_candidates(store=FakeStore(), request_id="req-1")

    assert candidates == []
