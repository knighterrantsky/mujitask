from __future__ import annotations

from typing import Any


class TaskRequestRepository:
    def __init__(self, store: Any):
        self._store = store

    def load(self, *, request_id: str) -> Any:
        return self._store._request_status_query.load_task_request(request_id=request_id)
