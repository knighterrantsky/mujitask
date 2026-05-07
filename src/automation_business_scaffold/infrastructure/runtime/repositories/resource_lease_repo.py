from __future__ import annotations

from typing import Any


class ResourceLeaseRepository:
    def __init__(self, store: Any):
        self._store = store

    def requeue_expired_leases(self, connection: Any, *, now: float) -> None:
        store = self._store
        expired_rows = (
            connection.execute(
                store._text(
                    """
                    SELECT lease.resource_code, lease.execution_id, lease.request_id, execution.status AS execution_status
                    FROM resource_lease lease
                    LEFT JOIN task_execution execution ON execution.execution_id = lease.execution_id
                    WHERE lease.lease_until <= :now
                    """
                ),
                {"now": now},
            )
            .mappings()
            .all()
        )
        for row in expired_rows:
            if str(row["execution_status"] or "") == "running":
                continue
            connection.execute(
                store._text("DELETE FROM resource_lease WHERE resource_code = :resource_code"),
                {"resource_code": row["resource_code"]},
            )
