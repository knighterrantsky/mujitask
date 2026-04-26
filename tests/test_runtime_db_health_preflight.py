from __future__ import annotations

from typing import Any

from automation_business_scaffold.control_plane.executor import runner


class UnhealthyStore:
    def collect_db_connection_health(
        self,
        *,
        max_connection_ratio: float = 0.8,
        max_idle_in_transaction: int = 0,
    ) -> dict[str, Any]:
        return {
            "status": "warning",
            "healthy": False,
            "max_connection_ratio": max_connection_ratio,
            "max_idle_in_transaction": max_idle_in_transaction,
            "warnings": ["connection_ratio_exceeded"],
        }

    def submit_task_request(self, **kwargs: Any) -> Any:
        raise AssertionError("submit_task_request should not be called when DB preflight fails")


def test_submit_rejects_when_runtime_db_connection_health_is_unhealthy(monkeypatch) -> None:
    monkeypatch.setattr(runner, "create_runtime_store", lambda settings: UnhealthyStore())

    payload = runner.submit_task_request(
        "refresh_current_competitor_table",
        {
            "execution_control_db_url": "postgresql+psycopg://pytest@/runtime?host=/tmp",
            "execution_control_db_health_max_connection_ratio": 0.7,
        },
    )

    assert payload["status"] == "failed"
    assert payload["request_status"] == "rejected"
    assert payload["error_type"] == "infrastructure"
    assert payload["error_code"] == "runtime_db_connection_unhealthy"
    assert payload["retryable"] is True
    assert payload["db_connection_health"]["max_connection_ratio"] == 0.7
