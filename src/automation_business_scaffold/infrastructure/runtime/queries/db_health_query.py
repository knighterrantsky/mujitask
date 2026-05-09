from __future__ import annotations

from typing import Any


class DbHealthQuery:
    def __init__(self, store: Any):
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def collect_db_connection_health(
        self,
        *,
        max_connection_ratio: float = 0.8,
        max_idle_in_transaction: int = -1,
    ) -> dict[str, Any]:
        threshold_ratio = min(max(float(max_connection_ratio or 0.8), 0.1), 1.0)
        idle_tx_threshold = int(max_idle_in_transaction if max_idle_in_transaction is not None else -1)
        with self._engine.connect() as connection:
            max_connections = int(
                connection.execute(
                    self._text("SELECT setting::int FROM pg_settings WHERE name = 'max_connections'")
                ).scalar_one()
                or 0
            )
            state_rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT COALESCE(state, '') AS state, count(*)::int AS count
                        FROM pg_stat_activity
                        GROUP BY COALESCE(state, '')
                        """
                    )
                )
                .mappings()
                .all()
            )
            source_rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT COALESCE(application_name, '') AS application_name,
                               COALESCE(state, '') AS state,
                               count(*)::int AS count
                        FROM pg_stat_activity
                        GROUP BY COALESCE(application_name, ''), COALESCE(state, '')
                        ORDER BY count(*) DESC, application_name, state
                        LIMIT 20
                        """
                    )
                )
                .mappings()
                .all()
            )
        counts_by_state = {str(row["state"] or "unknown"): int(row["count"] or 0) for row in state_rows}
        total_connections = sum(counts_by_state.values())
        connection_ratio = (total_connections / max_connections) if max_connections else 0.0
        idle_in_transaction_count = counts_by_state.get("idle in transaction", 0)
        warnings: list[str] = []
        if max_connections and connection_ratio >= threshold_ratio:
            warnings.append("connection_ratio_exceeded")
        if idle_tx_threshold >= 0 and idle_in_transaction_count > idle_tx_threshold:
            warnings.append("idle_in_transaction_exceeded")
        return {
            "status": "warning" if warnings else "ok",
            "healthy": not warnings,
            "max_connections": max_connections,
            "total_connections": total_connections,
            "connection_ratio": connection_ratio,
            "max_connection_ratio": threshold_ratio,
            "idle_in_transaction_count": idle_in_transaction_count,
            "max_idle_in_transaction": idle_tx_threshold,
            "counts_by_state": counts_by_state,
            "top_sources": [
                {
                    "application_name": str(row["application_name"] or ""),
                    "state": str(row["state"] or ""),
                    "count": int(row["count"] or 0),
                }
                for row in source_rows
            ],
            "warnings": warnings,
        }
