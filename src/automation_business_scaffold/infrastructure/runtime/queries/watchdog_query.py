from __future__ import annotations

from typing import Any, Mapping

from automation_business_scaffold.infrastructure.runtime.persistence_primitives import build_bind_placeholders


class WatchdogQuery:
    def __init__(self, store: Any):
        self._store = store

    def scan_runtime_rows(
        self,
        *,
        table_name: str,
        statuses: tuple[str, ...],
        predicate_sql: str,
        predicate_params: Mapping[str, Any],
        limit: int,
        order_by_sql: str,
    ) -> list[dict[str, Any]]:
        normalized_statuses = tuple(str(status or "").strip() for status in statuses if str(status or "").strip())
        if not normalized_statuses:
            return []
        placeholders, status_params = build_bind_placeholders("status", normalized_statuses)
        query = f"""
            SELECT *
            FROM {table_name}
            WHERE status IN ({placeholders})
              AND {predicate_sql}
            ORDER BY {order_by_sql}
            LIMIT :limit
        """
        params = dict(predicate_params)
        params.update(status_params)
        params["limit"] = max(int(limit or 1), 1)
        store = self._store
        with store._engine.connect() as connection:
            rows = connection.execute(store._text(query), params).mappings().all()
        return [dict(row) for row in rows]
