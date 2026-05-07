from __future__ import annotations

from automation_business_scaffold.models.artifact_object import ArtifactObjectRecord
from automation_business_scaffold.infrastructure.runtime.persistence_primitives import json_dumps as _json_dumps


class ArtifactObjectRepository:
    def __init__(self, store: Any):
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def replace_artifacts(self, *, run_id: str, records: list[ArtifactObjectRecord]) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                self._text("DELETE FROM artifact_object WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            for record in records:
                connection.execute(
                    self._text(
                        """
                        INSERT INTO artifact_object (
                            artifact_id, request_id, execution_id, run_id, step_id, kind,
                            bucket, object_key, etag, size, content_type, source_path,
                            metadata_json, created_at
                        ) VALUES (
                            :artifact_id, :request_id, :execution_id, :run_id, :step_id, :kind,
                            :bucket, :object_key, :etag, :size, :content_type, :source_path,
                            :metadata_json, :created_at
                        )
                        """
                    ),
                    {
                        "artifact_id": record.artifact_id,
                        "request_id": record.request_id,
                        "execution_id": record.execution_id,
                        "run_id": record.run_id,
                        "step_id": record.step_id,
                        "kind": record.kind,
                        "bucket": record.bucket,
                        "object_key": record.object_key,
                        "etag": record.etag,
                        "size": record.size,
                        "content_type": record.content_type,
                        "source_path": record.source_path,
                        "metadata_json": _json_dumps(record.metadata),
                        "created_at": record.created_at,
                    },
                )
