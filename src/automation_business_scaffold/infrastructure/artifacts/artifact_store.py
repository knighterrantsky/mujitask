from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    bucket: str
    object_key: str
    etag: str
    size: int
    content_type: str
    uri: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ArtifactStore(Protocol):
    provider_code: str

    def upload_file(
        self,
        *,
        bucket: str,
        object_key: str,
        local_path: Path,
        content_type: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> StoredArtifact: ...

    def build_uri(self, *, bucket: str, object_key: str) -> str: ...


def normalize_artifact_store_provider(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"", "local", "filesystem", "disabled", "none"}:
        return "local"
    return normalized


def normalize_object_prefix(prefix: str) -> str:
    return str(prefix or "").strip().strip("/")


def join_object_key(prefix: str, object_key: str) -> str:
    normalized_prefix = normalize_object_prefix(prefix)
    normalized_key = str(object_key or "").strip().lstrip("/")
    if not normalized_prefix:
        return normalized_key
    if not normalized_key:
        return normalized_prefix
    return f"{normalized_prefix}/{normalized_key}"


def create_artifact_store(settings: Mapping[str, Any]) -> ArtifactStore | None:
    provider = normalize_artifact_store_provider(settings.get("artifact_store_provider"))
    if provider == "local":
        return None
    if provider == "minio":
        from automation_business_scaffold.infrastructure.artifacts.minio_store import MinioArtifactStore

        return MinioArtifactStore(
            endpoint=str(settings.get("minio_endpoint") or "").strip(),
            access_key=str(settings.get("minio_access_key") or "").strip(),
            secret_key=str(settings.get("minio_secret_key") or "").strip(),
            secure=bool(settings.get("minio_secure", False)),
            region=str(settings.get("minio_region") or "").strip(),
            create_bucket=bool(settings.get("minio_create_bucket", False)),
        )
    raise ValueError(f"Unsupported artifact store provider '{provider}'.")
