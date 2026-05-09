from __future__ import annotations

import time
import uuid
from typing import Any, Mapping

from automation_business_scaffold.infrastructure.runtime.persistence_primitives import (
    coerce_float as _coerce_float,
    json_dumps as _json_dumps,
    load_json_dict as _load_json_dict,
)


class InfluencerPoolJobRepository:
    def __init__(self, store: Any):
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def upsert_influencer_pool_author_jobs(
        self,
        *,
        jobs: list[dict[str, Any]],
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        created_count = 0
        updated_count = 0
        kept_terminal_count = 0
        now = time.time()
        with self._engine.begin() as connection:
            for job in jobs:
                request_id = str(job.get("request_id", "") or "").strip()
                product_id = str(job.get("product_id", "") or "").strip()
                influencer_id = str(job.get("influencer_id", "") or "").strip()
                source_record_id = str(job.get("source_record_id", "") or "").strip()
                if not product_id or not influencer_id:
                    continue
                existing = (
                    connection.execute(
                        self._text(
                            """
                            SELECT *
                            FROM influencer_pool_author_job
                            WHERE request_id = :request_id
                              AND source_record_id = :source_record_id
                              AND product_id = :product_id
                              AND influencer_id = :influencer_id
                            LIMIT 1
                            """
                        ),
                        {
                            "request_id": request_id,
                            "source_record_id": source_record_id,
                            "product_id": product_id,
                            "influencer_id": influencer_id,
                        },
                    )
                    .mappings()
                    .first()
                )
                payload = {
                    "request_id": request_id,
                    "source_record_id": source_record_id,
                    "product_id": product_id,
                    "influencer_id": influencer_id,
                    "uid": str(job.get("uid", "") or ""),
                    "sold_count": _coerce_float(job.get("sold_count")),
                    "follower_count": _coerce_float(job.get("follower_count")),
                    "holiday_name": str(job.get("holiday_name", "") or ""),
                    "source_images_json": _json_dumps({"value": job.get("source_images")}),
                    "author_row_json": _json_dumps(
                        job.get("author_row") if isinstance(job.get("author_row"), dict) else {}
                    ),
                    "force_refresh": 1 if bool(job.get("force_refresh")) else 0,
                    "max_attempts": int(job.get("max_attempts", 3) or 3),
                }
                if existing is None:
                    connection.execute(
                        self._text(
                            """
                            INSERT INTO influencer_pool_author_job (
                                job_id, request_id, source_record_id, product_id, influencer_id, uid,
                                sold_count, follower_count, holiday_name, source_images_json,
                                author_row_json, force_refresh, status, stage, attempt_count, max_attempts,
                                available_at, created_at, updated_at
                            ) VALUES (
                                :job_id, :request_id, :source_record_id, :product_id, :influencer_id, :uid,
                                :sold_count, :follower_count, :holiday_name, :source_images_json,
                                :author_row_json, :force_refresh, 'pending', 'queued', 0, :max_attempts,
                                :available_at, :created_at, :updated_at
                            )
                            """
                        ),
                        {
                            **payload,
                            "job_id": uuid.uuid4().hex,
                            "available_at": now,
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
                    created_count += 1
                    continue

                existing_status = str(existing["status"] or "")
                should_keep_terminal = (
                    existing_status in {"succeeded", "skipped"}
                    and not force_refresh
                )
                next_status = existing_status if should_keep_terminal else "pending"
                if should_keep_terminal:
                    kept_terminal_count += 1
                else:
                    updated_count += 1
                connection.execute(
                    self._text(
                        """
                        UPDATE influencer_pool_author_job
                        SET request_id = :request_id,
                            source_record_id = :source_record_id,
                            uid = :uid,
                            sold_count = :sold_count,
                            follower_count = :follower_count,
                            holiday_name = :holiday_name,
                            source_images_json = :source_images_json,
                            author_row_json = :author_row_json,
                            force_refresh = :force_refresh,
                            status = :status,
                            stage = CASE WHEN :status = status THEN stage ELSE 'queued' END,
                            max_attempts = :max_attempts,
                            available_at = CASE WHEN :status = status THEN available_at ELSE :available_at END,
                            worker_id = CASE WHEN :status = status THEN worker_id ELSE '' END,
                            lease_until = CASE WHEN :status = status THEN lease_until ELSE NULL END,
                            updated_at = :updated_at
                        WHERE job_id = :job_id
                        """
                    ),
                    {
                        **payload,
                        "job_id": existing["job_id"],
                        "status": next_status,
                        "available_at": now,
                        "updated_at": now,
                    },
                )
        return {
            "created_count": created_count,
            "updated_count": updated_count,
            "kept_terminal_count": kept_terminal_count,
        }

    def upsert_influencer_pool_product_jobs(
        self,
        *,
        jobs: list[dict[str, Any]],
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        created_count = 0
        updated_count = 0
        kept_terminal_count = 0
        now = time.time()
        with self._engine.begin() as connection:
            for job in jobs:
                request_id = str(job.get("request_id", "") or "").strip()
                source_record_id = str(job.get("source_record_id", "") or "").strip()
                product_id = str(job.get("product_id", "") or "").strip()
                if not source_record_id:
                    continue
                existing = (
                    connection.execute(
                        self._text(
                            """
                            SELECT *
                            FROM influencer_pool_product_job
                            WHERE request_id = :request_id
                              AND source_record_id = :source_record_id
                              AND product_id = :product_id
                            LIMIT 1
                            """
                        ),
                        {
                            "request_id": request_id,
                            "source_record_id": source_record_id,
                            "product_id": product_id,
                        },
                    )
                    .mappings()
                    .first()
                )
                payload = {
                    "request_id": request_id,
                    "source_record_id": source_record_id,
                    "product_id": product_id,
                    "source_record_json": _json_dumps(
                        job.get("source_record") if isinstance(job.get("source_record"), dict) else {}
                    ),
                    "max_attempts": int(job.get("max_attempts", 3) or 3),
                }
                if existing is None:
                    connection.execute(
                        self._text(
                            """
                            INSERT INTO influencer_pool_product_job (
                                job_id, request_id, source_record_id, product_id, source_record_json,
                                status, stage, attempt_count, max_attempts,
                                available_at, created_at, updated_at
                            ) VALUES (
                                :job_id, :request_id, :source_record_id, :product_id, :source_record_json,
                                'pending', 'queued', 0, :max_attempts,
                                :available_at, :created_at, :updated_at
                            )
                            """
                        ),
                        {
                            **payload,
                            "job_id": uuid.uuid4().hex,
                            "available_at": now,
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
                    created_count += 1
                    continue

                existing_status = str(existing["status"] or "")
                should_keep_terminal = existing_status in {"completed", "skipped"} and not force_refresh
                next_status = existing_status if should_keep_terminal else "pending"
                if should_keep_terminal:
                    kept_terminal_count += 1
                else:
                    updated_count += 1
                connection.execute(
                    self._text(
                        """
                        UPDATE influencer_pool_product_job
                        SET request_id = :request_id,
                            product_id = :product_id,
                            source_record_json = :source_record_json,
                            status = :status,
                            stage = CASE WHEN :status = status THEN stage ELSE 'queued' END,
                            max_attempts = :max_attempts,
                            available_at = CASE WHEN :status = status THEN available_at ELSE :available_at END,
                            worker_id = CASE WHEN :status = status THEN worker_id ELSE '' END,
                            lease_until = CASE WHEN :status = status THEN lease_until ELSE NULL END,
                            updated_at = :updated_at
                        WHERE job_id = :job_id
                        """
                    ),
                    {
                        **payload,
                        "job_id": existing["job_id"],
                        "status": next_status,
                        "available_at": now,
                        "updated_at": now,
                    },
                )
        return {
            "created_count": created_count,
            "updated_count": updated_count,
            "kept_terminal_count": kept_terminal_count,
        }

    def claim_influencer_pool_product_job(
        self,
        *,
        request_id: str = "",
        worker_id: str,
        lease_seconds: float,
    ) -> dict[str, Any] | None:
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_product_job
                    SET status = 'failed_retry',
                        stage = 'lease_expired',
                        worker_id = '',
                        lease_until = NULL,
                        updated_at = :updated_at
                    WHERE status IN ('discovering')
                      AND lease_until IS NOT NULL
                      AND lease_until <= :now
                    """
                ),
                {"now": now, "updated_at": now},
            )
            row = (
                connection.execute(
                    self._text(
                        """
                        SELECT *
                        FROM influencer_pool_product_job
                        WHERE (:request_id = '' OR request_id = :request_id)
                          AND status IN ('pending', 'failed_retry')
                          AND available_at <= :available_at
                        ORDER BY created_at ASC, updated_at ASC
                        LIMIT 1
                        """
                    ),
                    {"request_id": request_id, "available_at": now},
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_product_job
                    SET status = 'discovering',
                        stage = 'product_author_list',
                        attempt_count = COALESCE(attempt_count, 0) + 1,
                        worker_id = :worker_id,
                        lease_until = :lease_until,
                        started_at = CASE WHEN started_at IS NULL THEN :now ELSE started_at END,
                        heartbeat_at = :now,
                        updated_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {
                    "job_id": row["job_id"],
                    "worker_id": worker_id,
                    "lease_until": now + max(lease_seconds, 5.0),
                    "now": now,
                },
            )
            claimed = (
                connection.execute(
                    self._text("SELECT * FROM influencer_pool_product_job WHERE job_id = :job_id"),
                    {"job_id": row["job_id"]},
                )
                .mappings()
                .first()
            )
            return self._influencer_pool_product_job_from_row(claimed) if claimed is not None else None

    def mark_influencer_pool_product_job_discovered(
        self,
        *,
        job_id: str,
        run_id: str,
        matched_author_count: int = 0,
        queued_author_job_count: int = 0,
    ) -> None:
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_product_job
                    SET status = 'detail_pending',
                        stage = 'author_jobs_queued',
                        matched_author_count = :matched_author_count,
                        queued_author_job_count = :queued_author_job_count,
                        run_id = :run_id,
                        worker_id = '',
                        lease_until = NULL,
                        heartbeat_at = :now,
                        updated_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "matched_author_count": max(int(matched_author_count or 0), 0),
                    "queued_author_job_count": max(int(queued_author_job_count or 0), 0),
                    "now": now,
                },
            )

    def mark_influencer_pool_product_job_success(
        self,
        *,
        job_id: str,
        run_id: str,
        stage: str = "completed",
    ) -> None:
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_product_job
                    SET status = 'completed',
                        stage = :stage,
                        run_id = :run_id,
                        worker_id = '',
                        lease_until = NULL,
                        heartbeat_at = :now,
                        updated_at = :now,
                        finished_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id, "run_id": run_id, "stage": stage, "now": now},
            )

    def mark_influencer_pool_product_job_author_retry_wait(
        self,
        *,
        job_id: str,
        run_id: str,
        error_text: str = "",
        error_type: str = "",
        error_code: str = "",
        error_path: str = "",
    ) -> None:
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_product_job
                    SET status = 'author_failed_retry',
                        stage = 'waiting_author_retry',
                        run_id = :run_id,
                        last_error_text = :error_text,
                        last_error_type = :error_type,
                        last_error_code = :error_code,
                        last_error_path = :error_path,
                        worker_id = '',
                        lease_until = NULL,
                        heartbeat_at = :now,
                        updated_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "error_text": error_text,
                    "error_type": error_type,
                    "error_code": error_code,
                    "error_path": error_path,
                    "now": now,
                },
            )

    def reactivate_influencer_pool_product_job_finalizer(
        self,
        *,
        request_id: str = "",
        source_record_id: str,
        product_id: str,
        run_id: str,
    ) -> None:
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_product_job
                    SET status = 'detail_pending',
                        stage = 'author_job_updated',
                        run_id = :run_id,
                        worker_id = '',
                        lease_until = NULL,
                        heartbeat_at = :now,
                        updated_at = :now
                    WHERE source_record_id = :source_record_id
                      AND product_id = :product_id
                      AND (:request_id = '' OR request_id = :request_id)
                      AND status IN ('detail_pending', 'author_failed_retry')
                    """
                ),
                {
                    "request_id": request_id,
                    "source_record_id": source_record_id,
                    "product_id": product_id,
                    "run_id": run_id,
                    "now": now,
                },
            )

    def mark_influencer_pool_product_job_failed(
        self,
        *,
        job_id: str,
        run_id: str,
        error_text: str,
        error_type: str = "",
        error_code: str = "",
        error_path: str = "",
        stage: str = "",
        retry_delay_seconds: float = 30.0,
        hard_stop: bool = False,
    ) -> None:
        now = time.time()
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    self._text("SELECT attempt_count, max_attempts FROM influencer_pool_product_job WHERE job_id = :job_id"),
                    {"job_id": job_id},
                )
                .mappings()
                .first()
            )
            attempt_count = int(row["attempt_count"] or 0) if row is not None else 0
            max_attempts = int(row["max_attempts"] or 1) if row is not None else 1
            status = "hard_stopped" if hard_stop else ("failed_retry" if attempt_count < max_attempts else "hard_failed")
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_product_job
                    SET status = :status,
                        stage = :stage,
                        run_id = :run_id,
                        last_error_text = :error_text,
                        last_error_type = :error_type,
                        last_error_code = :error_code,
                        last_error_path = :error_path,
                        worker_id = '',
                        lease_until = NULL,
                        available_at = :available_at,
                        heartbeat_at = :now,
                        updated_at = :now,
                        finished_at = CASE WHEN :status IN ('hard_failed', 'hard_stopped') THEN :now ELSE finished_at END
                    WHERE job_id = :job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "status": status,
                    "stage": stage,
                    "run_id": run_id,
                    "error_text": error_text,
                    "error_type": error_type,
                    "error_code": error_code,
                    "error_path": error_path,
                    "available_at": now + max(retry_delay_seconds, 0.1),
                    "now": now,
                },
            )

    def list_influencer_pool_product_jobs_for_finalizer(
        self,
        *,
        request_id: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT *
                        FROM influencer_pool_product_job
                        WHERE (:request_id = '' OR request_id = :request_id)
                          AND status = 'detail_pending'
                        ORDER BY updated_at ASC, created_at ASC
                        LIMIT :limit
                        """
                    ),
                    {"request_id": request_id, "limit": max(int(limit or 1), 1)},
                )
                .mappings()
                .all()
            )
        return [self._influencer_pool_product_job_from_row(row) for row in rows]

    def list_influencer_pool_product_jobs_for_request(self, *, request_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT *
                        FROM influencer_pool_product_job
                        WHERE request_id = :request_id
                        ORDER BY created_at ASC, updated_at ASC
                        """
                    ),
                    {"request_id": request_id},
                )
                .mappings()
                .all()
            )
        return [self._influencer_pool_product_job_from_row(row) for row in rows]

    def summarize_influencer_pool_product_jobs_for_request(self, *, request_id: str) -> dict[str, Any]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT status, COUNT(*) AS count
                        FROM influencer_pool_product_job
                        WHERE request_id = :request_id
                        GROUP BY status
                        """
                    ),
                    {"request_id": request_id},
                )
                .mappings()
                .all()
            )
            aggregate = (
                connection.execute(
                    self._text(
                        """
                        SELECT
                            COALESCE(SUM(matched_author_count), 0) AS matched_author_count,
                            COALESCE(SUM(queued_author_job_count), 0) AS queued_author_job_count
                        FROM influencer_pool_product_job
                        WHERE request_id = :request_id
                        """
                    ),
                    {"request_id": request_id},
                )
                .mappings()
                .first()
            )
        counts = {str(row["status"]): int(row["count"] or 0) for row in rows}
        active_statuses = {
            "pending",
            "failed_retry",
            "discovering",
            "detail_pending",
            "author_failed_retry",
        }
        failed_statuses = {"hard_failed", "hard_stopped"}
        success_statuses = {"completed", "skipped"}
        total = sum(counts.values())
        active_count = sum(counts.get(status, 0) for status in active_statuses)
        failed_count = sum(counts.get(status, 0) for status in failed_statuses)
        success_count = sum(counts.get(status, 0) for status in success_statuses)
        return {
            "total": total,
            "counts": counts,
            "active_count": active_count,
            "terminal_count": max(total - active_count, 0),
            "success_count": success_count,
            "failed_count": failed_count,
            "matched_author_count": int((aggregate or {}).get("matched_author_count") or 0),
            "queued_author_job_count": int((aggregate or {}).get("queued_author_job_count") or 0),
        }

    def find_next_influencer_pool_work_request_id(
        self,
        *,
        task_code: str = "sync_tk_influencer_pool",
    ) -> str:
        now = time.time()
        queries = [
            """
            SELECT job.request_id
            FROM influencer_pool_product_job job
            JOIN task_request request ON request.request_id = job.request_id
            WHERE request.task_code = :task_code
              AND request.status = 'waiting'
              AND job.status IN ('pending', 'failed_retry')
              AND job.available_at <= :available_at
            ORDER BY job.available_at ASC, job.created_at ASC
            LIMIT 1
            """,
            """
            SELECT job.request_id
            FROM influencer_pool_author_job job
            JOIN task_request request ON request.request_id = job.request_id
            WHERE request.task_code = :task_code
              AND request.status = 'waiting'
              AND job.status IN ('pending', 'failed_retry')
              AND job.available_at <= :available_at
            ORDER BY job.available_at ASC, job.created_at ASC
            LIMIT 1
            """,
            """
            SELECT job.request_id
            FROM influencer_pool_product_job job
            JOIN task_request request ON request.request_id = job.request_id
            WHERE request.task_code = :task_code
              AND request.status = 'waiting'
              AND job.status = 'detail_pending'
            ORDER BY job.updated_at ASC, job.created_at ASC
            LIMIT 1
            """,
        ]
        with self._engine.connect() as connection:
            for query in queries:
                row = (
                    connection.execute(
                        self._text(query),
                        {"task_code": task_code, "available_at": now},
                    )
                    .mappings()
                    .first()
                )
                if row is not None:
                    return str(row["request_id"] or "")
        return ""

    def _influencer_pool_product_job_from_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "job_id": str(row["job_id"]),
            "request_id": str(row["request_id"] or ""),
            "source_record_id": str(row["source_record_id"] or ""),
            "product_id": str(row["product_id"] or ""),
            "source_record": _load_json_dict(row["source_record_json"]),
            "status": str(row["status"] or ""),
            "stage": str(row["stage"] or ""),
            "attempt_count": int(row["attempt_count"] or 0),
            "max_attempts": int(row["max_attempts"] or 0),
            "matched_author_count": int(row["matched_author_count"] or 0),
            "queued_author_job_count": int(row["queued_author_job_count"] or 0),
            "last_error_text": str(row["last_error_text"] or ""),
            "last_error_type": str(row["last_error_type"] or ""),
            "last_error_code": str(row["last_error_code"] or ""),
            "last_error_path": str(row["last_error_path"] or ""),
            "run_id": str(row["run_id"] or ""),
        }

    def claim_influencer_pool_author_job(
        self,
        *,
        request_id: str = "",
        product_id: str = "",
        source_record_id: str = "",
        worker_id: str,
        lease_seconds: float,
    ) -> dict[str, Any] | None:
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_author_job
                    SET status = 'failed_retry',
                        stage = 'lease_expired',
                        worker_id = '',
                        lease_until = NULL,
                        updated_at = :updated_at
                    WHERE status = 'running'
                      AND lease_until IS NOT NULL
                      AND lease_until <= :now
                    """
                ),
                {"now": now, "updated_at": now},
            )
            row = (
                connection.execute(
                    self._text(
                        """
                        SELECT *
                        FROM influencer_pool_author_job
                        WHERE (:request_id = '' OR request_id = :request_id)
                          AND (:product_id = '' OR product_id = :product_id)
                          AND (:source_record_id = '' OR source_record_id = :source_record_id)
                          AND status IN ('pending', 'failed_retry')
                          AND available_at <= :available_at
                        ORDER BY created_at ASC, updated_at ASC
                        LIMIT 1
                        """
                    ),
                    {
                        "request_id": request_id,
                        "product_id": product_id,
                        "source_record_id": source_record_id,
                        "available_at": now,
                    },
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_author_job
                    SET status = 'running',
                        stage = 'author_detail',
                        attempt_count = COALESCE(attempt_count, 0) + 1,
                        worker_id = :worker_id,
                        lease_until = :lease_until,
                        started_at = CASE WHEN started_at IS NULL THEN :now ELSE started_at END,
                        heartbeat_at = :now,
                        updated_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {
                    "job_id": row["job_id"],
                    "worker_id": worker_id,
                    "lease_until": now + max(lease_seconds, 5.0),
                    "now": now,
                },
            )
            claimed = (
                connection.execute(
                    self._text("SELECT * FROM influencer_pool_author_job WHERE job_id = :job_id"),
                    {"job_id": row["job_id"]},
                )
                .mappings()
                .first()
            )
            return self._influencer_pool_author_job_from_row(claimed) if claimed is not None else None

    def mark_influencer_pool_author_job_success(
        self,
        *,
        job_id: str,
        run_id: str,
        target_record_id: str = "",
        snapshot_id: str = "",
    ) -> None:
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_author_job
                    SET status = 'succeeded',
                        stage = 'completed',
                        target_record_id = :target_record_id,
                        snapshot_id = :snapshot_id,
                        run_id = :run_id,
                        worker_id = '',
                        lease_until = NULL,
                        heartbeat_at = :now,
                        updated_at = :now,
                        finished_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "target_record_id": target_record_id,
                    "snapshot_id": snapshot_id,
                    "now": now,
                },
            )

    def mark_influencer_pool_author_job_skipped(
        self,
        *,
        job_id: str,
        run_id: str,
        stage: str,
        reason: str,
    ) -> None:
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_author_job
                    SET status = 'skipped',
                        stage = :stage,
                        run_id = :run_id,
                        last_error_text = :reason,
                        worker_id = '',
                        lease_until = NULL,
                        heartbeat_at = :now,
                        updated_at = :now,
                        finished_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id, "run_id": run_id, "stage": stage, "reason": reason, "now": now},
            )

    def mark_influencer_pool_author_job_failed(
        self,
        *,
        job_id: str,
        run_id: str,
        error_text: str,
        error_type: str = "",
        error_code: str = "",
        error_path: str = "",
        stage: str = "",
        retry_delay_seconds: float = 30.0,
    ) -> None:
        now = time.time()
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    self._text("SELECT attempt_count, max_attempts FROM influencer_pool_author_job WHERE job_id = :job_id"),
                    {"job_id": job_id},
                )
                .mappings()
                .first()
            )
            attempt_count = int(row["attempt_count"] or 0) if row is not None else 0
            max_attempts = int(row["max_attempts"] or 1) if row is not None else 1
            status = "failed_retry" if attempt_count < max_attempts else "hard_failed"
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_author_job
                    SET status = :status,
                        stage = :stage,
                        run_id = :run_id,
                        last_error_text = :error_text,
                        last_error_type = :error_type,
                        last_error_code = :error_code,
                        last_error_path = :error_path,
                        worker_id = '',
                        lease_until = NULL,
                        available_at = :available_at,
                        heartbeat_at = :now,
                        updated_at = :now,
                        finished_at = CASE WHEN :status = 'hard_failed' THEN :now ELSE finished_at END
                    WHERE job_id = :job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "status": status,
                    "stage": stage,
                    "run_id": run_id,
                    "error_text": error_text,
                    "error_type": error_type,
                    "error_code": error_code,
                    "error_path": error_path,
                    "available_at": now + max(retry_delay_seconds, 0.1),
                    "now": now,
                },
            )

    def summarize_influencer_pool_author_jobs(
        self,
        *,
        request_id: str = "",
        product_id: str,
        source_record_id: str,
    ) -> dict[str, Any]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT status, COUNT(*) AS count
                        FROM influencer_pool_author_job
                        WHERE (:request_id = '' OR request_id = :request_id)
                          AND product_id = :product_id
                          AND source_record_id = :source_record_id
                        GROUP BY status
                        """
                    ),
                    {
                        "request_id": request_id,
                        "product_id": product_id,
                        "source_record_id": source_record_id,
                    },
                )
                .mappings()
                .all()
            )
        counts = {str(row["status"]): int(row["count"] or 0) for row in rows}
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

    def _influencer_pool_author_job_from_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "job_id": str(row["job_id"]),
            "request_id": str(row["request_id"] or ""),
            "source_record_id": str(row["source_record_id"] or ""),
            "product_id": str(row["product_id"] or ""),
            "influencer_id": str(row["influencer_id"] or ""),
            "uid": str(row["uid"] or ""),
            "sold_count": _coerce_float(row["sold_count"]),
            "follower_count": _coerce_float(row["follower_count"]),
            "holiday_name": str(row["holiday_name"] or ""),
            "source_images": _load_json_dict(row["source_images_json"]).get("value"),
            "author_row": _load_json_dict(row["author_row_json"]),
            "force_refresh": bool(int(row["force_refresh"] or 0)),
            "status": str(row["status"] or ""),
            "stage": str(row["stage"] or ""),
            "attempt_count": int(row["attempt_count"] or 0),
            "max_attempts": int(row["max_attempts"] or 0),
            "target_record_id": str(row["target_record_id"] or ""),
            "snapshot_id": str(row["snapshot_id"] or ""),
            "last_error_text": str(row["last_error_text"] or ""),
            "last_error_type": str(row["last_error_type"] or ""),
            "last_error_code": str(row["last_error_code"] or ""),
            "last_error_path": str(row["last_error_path"] or ""),
            "run_id": str(row["run_id"] or ""),
        }
