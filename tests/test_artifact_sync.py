from __future__ import annotations

import hashlib
from pathlib import Path

from automation_business_scaffold.infrastructure.artifacts.artifact_store import (
    StoredArtifact,
)
from automation_business_scaffold.infrastructure.artifacts.artifact_sync import (
    ArtifactFileSpec,
    sync_artifact_specs,
)


class _RecordingStore:
    provider_code = "minio"

    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, bytes]] = []

    def upload_file(
        self,
        *,
        bucket: str,
        object_key: str,
        local_path: Path,
        content_type: str,
        metadata: dict[str, str],
    ) -> StoredArtifact:
        payload = local_path.read_bytes()
        self.uploads.append((bucket, object_key, payload))
        return StoredArtifact(
            bucket=bucket,
            object_key=object_key,
            etag=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
            content_type=content_type,
            uri=f"s3://{bucket}/{object_key}",
            metadata=metadata,
        )


def test_generic_runtime_artifact_remains_local_when_minio_is_configured(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.log"
    path.write_bytes(b"runtime diagnostic")
    store = _RecordingStore()

    records, uri_prefix = sync_artifact_specs(
        run_id="run-1",
        request_id="request-1",
        execution_id="execution-1",
        artifact_root=tmp_path,
        artifact_bucket="business-assets",
        artifact_object_prefix="mujitask",
        specs=[
            ArtifactFileSpec(
                kind="stdout_log",
                step_id="runtime",
                relative_name="stdout.log",
                path=path,
                content_type="text/plain",
            )
        ],
        artifact_store=store,
        created_at=1.0,
    )

    assert store.uploads == []
    assert uri_prefix == ""
    assert records[0].metadata["storage_backend"] == "local"
    assert records[0].metadata["local_uri"].startswith("file://")
    assert "remote_uri" not in records[0].metadata


def test_explicit_business_object_key_is_the_only_minio_admission_path(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.jpg"
    path.write_bytes(b"business image")
    store = _RecordingStore()

    records, _ = sync_artifact_specs(
        run_id="run-1",
        request_id="request-1",
        execution_id="execution-1",
        artifact_root=tmp_path,
        artifact_bucket="business-assets",
        artifact_object_prefix="mujitask",
        specs=[
            ArtifactFileSpec(
                kind="product_main_image",
                step_id="media_asset_sync",
                relative_name="assets/main.jpg",
                path=path,
                content_type="image/jpeg",
                object_key="product-media/123/main.jpg",
            )
        ],
        artifact_store=store,
        created_at=1.0,
    )

    assert store.uploads == [
        (
            "business-assets",
            "mujitask/product-media/123/main.jpg",
            b"business image",
        )
    ]
    assert records[0].metadata["storage_backend"] == "minio"
    assert records[0].metadata["remote_uri"] == (
        "s3://business-assets/mujitask/product-media/123/main.jpg"
    )
