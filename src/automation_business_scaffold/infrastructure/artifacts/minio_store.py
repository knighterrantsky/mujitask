from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from automation_business_scaffold.infrastructure.artifacts.artifact_store import StoredArtifact


class MinioArtifactStore:
    provider_code = "minio"

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        secure: bool = False,
        region: str = "",
        create_bucket: bool = False,
    ):
        self._endpoint = str(endpoint or "").strip()
        self._access_key = str(access_key or "").strip()
        self._secret_key = str(secret_key or "").strip()
        self._secure = bool(secure)
        self._region = str(region or "").strip()
        self._create_bucket = bool(create_bucket)
        if not self._endpoint:
            raise ValueError(
                "MinIO artifact store requires minio_endpoint. Fill "
                "BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT / EXECUTION_CONTROL_MINIO_ENDPOINT "
                "in scripts/execution_control/executor.local.env."
            )
        if not self._access_key or not self._secret_key:
            raise ValueError(
                "MinIO artifact store requires minio_access_key and minio_secret_key. Fill "
                "BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY / SECRET_KEY in "
                "scripts/execution_control/executor.local.env."
            )
        try:
            from minio import Minio
        except Exception as exc:
            raise RuntimeError(
                "The 'minio' package is required for artifact_store_provider=minio."
            ) from exc
        self._client = Minio(
            self._endpoint,
            access_key=self._access_key,
            secret_key=self._secret_key,
            secure=self._secure,
            region=self._region or None,
        )

    def _ensure_bucket(self, bucket: str) -> None:
        if not self._create_bucket:
            return
        if self._client.bucket_exists(bucket):
            return
        self._client.make_bucket(bucket, location=self._region or None)

    @staticmethod
    def _normalize_metadata(metadata: Mapping[str, Any] | None) -> dict[str, str]:
        if not metadata:
            return {}
        normalized: dict[str, str] = {}
        for key, value in metadata.items():
            normalized_key = str(key or "").strip()
            if not normalized_key:
                continue
            normalized[normalized_key] = str(value if value is not None else "")
        return normalized

    def build_uri(self, *, bucket: str, object_key: str) -> str:
        return f"s3://{bucket}/{object_key}"

    def upload_file(
        self,
        *,
        bucket: str,
        object_key: str,
        local_path: Path,
        content_type: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> StoredArtifact:
        self._ensure_bucket(bucket)
        result = self._client.fput_object(
            bucket,
            object_key,
            str(local_path),
            content_type=content_type or "application/octet-stream",
            metadata=self._normalize_metadata(metadata) or None,
        )
        stat_result = local_path.stat()
        return StoredArtifact(
            bucket=bucket,
            object_key=object_key,
            etag=str(getattr(result, "etag", "") or ""),
            size=int(stat_result.st_size),
            content_type=content_type or "application/octet-stream",
            uri=self.build_uri(bucket=bucket, object_key=object_key),
            metadata={
                "storage_backend": self.provider_code,
                "endpoint": self._endpoint,
                "secure": self._secure,
            },
        )
