from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from automation_business_scaffold.infrastructure.artifacts.artifact_store import (
    ArtifactStore,
    create_artifact_store,
    join_object_key,
)
from automation_business_scaffold.models import ArtifactObjectRecord


@dataclass(frozen=True, slots=True)
class ArtifactFileSpec:
    kind: str
    step_id: str
    relative_name: str
    path: Path
    content_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    object_key: str = ""


def runtime_object_key(run_id: str, relative_name: str) -> str:
    normalized_relative_name = str(relative_name or "").strip().lstrip("/")
    return f"runs/{run_id}/{normalized_relative_name}"


def artifact_content_type(kind: str, path: Path) -> str:
    if kind.endswith("_json") or path.suffix == ".json":
        return "application/json"
    if kind.endswith("_log") or path.suffix == ".log":
        return "text/plain"
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def build_artifact_payload(
    *,
    artifact_root: Path,
    run_id: str,
    records: list[ArtifactObjectRecord],
    artifact_uri_prefix: str = "",
) -> dict[str, Any]:
    by_kind = {record.kind: record for record in records}
    run_prefix = artifact_root / "runs" / run_id
    provider = "local"
    if records:
        provider = str(records[0].metadata.get("storage_backend", "local") or "local")
    return {
        "artifact_count": len(records),
        "artifacts": [record.to_dict() for record in records],
        "artifact_uri_prefix": artifact_uri_prefix or (run_prefix.resolve().as_uri() if records else ""),
        "run_object_key": by_kind.get("run_json").object_key if "run_json" in by_kind else "",
        "steps_object_key": by_kind.get("steps_json").object_key if "steps_json" in by_kind else "",
        "signals_object_key": by_kind.get("signals_json").object_key if "signals_json" in by_kind else "",
        "stdout_object_key": by_kind.get("stdout_log").object_key if "stdout_log" in by_kind else "",
        "artifacts_dir": str((run_prefix / "artifacts").resolve()) if records else "",
        "artifact_store_provider": provider,
    }


def sync_artifact_specs(
    *,
    run_id: str,
    request_id: str,
    execution_id: str,
    artifact_root: Path,
    artifact_bucket: str,
    artifact_object_prefix: str,
    specs: list[ArtifactFileSpec],
    artifact_store: ArtifactStore | None = None,
    created_at: float,
) -> tuple[list[ArtifactObjectRecord], str]:
    records: list[ArtifactObjectRecord] = []
    artifact_uri_prefix = ""
    for spec in specs:
        resolved_path = spec.path.expanduser()
        if not resolved_path.exists() or not resolved_path.is_file():
            continue
        explicit_object_key = str(spec.object_key or "").strip().lstrip("/")
        local_object_key = explicit_object_key or runtime_object_key(run_id, spec.relative_name)
        content_type = spec.content_type or artifact_content_type(spec.kind, resolved_path)
        metadata = dict(spec.metadata)
        metadata.setdefault("local_object_key", local_object_key)
        metadata.setdefault("local_uri", resolved_path.resolve().as_uri())
        metadata.setdefault("storage_backend", getattr(artifact_store, "provider_code", "local"))
        bucket = artifact_bucket
        object_key = local_object_key
        etag = _sha256_of_file(resolved_path)
        size = resolved_path.stat().st_size
        if artifact_store is not None:
            object_key = join_object_key(artifact_object_prefix, local_object_key)
            uploaded = artifact_store.upload_file(
                bucket=bucket,
                object_key=object_key,
                local_path=resolved_path,
                content_type=content_type,
                metadata={
                    "request_id": request_id,
                    "execution_id": execution_id,
                    "run_id": run_id,
                    "step_id": spec.step_id,
                    "kind": spec.kind,
                },
            )
            bucket = uploaded.bucket
            object_key = uploaded.object_key
            etag = uploaded.etag or etag
            size = uploaded.size or size
            content_type = uploaded.content_type or content_type
            metadata.update(uploaded.metadata)
            metadata["remote_uri"] = uploaded.uri
            if not explicit_object_key:
                artifact_uri_prefix = artifact_store.build_uri(
                    bucket=bucket,
                    object_key=join_object_key(artifact_object_prefix, f"runs/{run_id}"),
                )
        record = ArtifactObjectRecord(
            artifact_id=_build_artifact_id(run_id, spec.kind, resolved_path),
            request_id=request_id,
            execution_id=execution_id,
            run_id=run_id,
            step_id=spec.step_id,
            kind=spec.kind,
            bucket=bucket,
            object_key=object_key,
            etag=etag,
            size=int(size),
            content_type=content_type,
            source_path=str(resolved_path.resolve()),
            metadata=metadata,
            created_at=created_at,
        )
        records.append(record)
    return records, artifact_uri_prefix


def collect_referenced_artifact_specs(
    *,
    result_payload: dict[str, Any],
    step_id: str,
) -> list[ArtifactFileSpec]:
    specs: list[ArtifactFileSpec] = []
    path_to_index: dict[str, int] = {}
    used_relative_names: set[str] = set()

    def add_path(
        *,
        raw_path: str,
        kind_hint: str,
        file_name: str = "",
        content_type: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        normalized_path = str(raw_path or "").strip()
        if not normalized_path:
            return
        path = Path(normalized_path).expanduser()
        if not path.exists() or not path.is_file():
            return
        resolved = str(path.resolve())
        kind = _normalize_kind(kind_hint)
        existing_index = path_to_index.get(resolved)
        if existing_index is not None:
            existing = specs[existing_index]
            if _kind_specificity(kind) <= _kind_specificity(existing.kind):
                return
            relative_name = _build_unique_relative_name(
                used_relative_names,
                kind=kind,
                file_name=file_name or path.name,
            )
            specs[existing_index] = ArtifactFileSpec(
                kind=kind,
                step_id=step_id,
                relative_name=relative_name,
                path=path,
                content_type=content_type,
                metadata=metadata or {},
            )
            return
        relative_name = _build_unique_relative_name(
            used_relative_names,
            kind=kind,
            file_name=file_name or path.name,
        )
        path_to_index[resolved] = len(specs)
        specs.append(
            ArtifactFileSpec(
                kind=kind,
                step_id=step_id,
                relative_name=relative_name,
                path=path,
                content_type=content_type,
                metadata=metadata or {},
            )
        )

    def walk(node: Any, hint: str = "") -> None:
        if isinstance(node, dict):
            if str(node.get("type") or "").strip() == "local_file":
                add_path(
                    raw_path=str(node.get("path") or ""),
                    kind_hint=hint or "local_file",
                    file_name=str(node.get("file_name") or ""),
                    content_type=str(node.get("mime_type") or ""),
                    metadata={
                        "reference_origin": "local_file",
                        "source_url": str(node.get("source_url") or ""),
                    },
                )
            for key, value in node.items():
                if key.endswith("_local_path"):
                    base_name = key[: -len("_local_path")] or "local_file"
                    add_path(
                        raw_path=str(value or ""),
                        kind_hint=f"{base_name}_file",
                        file_name=str(node.get(f"{base_name}_file_name") or ""),
                        content_type=str(node.get(f"{base_name}_mime_type") or ""),
                        metadata={
                            "reference_origin": "logical_field",
                            "reference_field": key,
                        },
                    )
                walk(value, hint=key)
            return
        if isinstance(node, list):
            for item in node:
                walk(item, hint=hint)

    walk(result_payload)
    return specs


def create_store_from_settings(settings: dict[str, Any]) -> ArtifactStore | None:
    return create_artifact_store(settings)


def _sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _build_artifact_id(run_id: str, kind: str, path: Path) -> str:
    seed = f"{run_id}:{kind}:{path.resolve()}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def _normalize_kind(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", str(value or "").strip()).strip("_").lower()
    return normalized or "referenced_file"


def _build_unique_relative_name(
    used_relative_names: set[str],
    *,
    kind: str,
    file_name: str,
) -> str:
    base_name = Path(file_name or "artifact.bin").name or "artifact.bin"
    stem = Path(base_name).stem or "artifact"
    suffix = Path(base_name).suffix
    candidate = f"referenced/{kind}/{base_name}"
    index = 2
    while candidate in used_relative_names:
        candidate = f"referenced/{kind}/{stem}-{index}{suffix}"
        index += 1
    used_relative_names.add(candidate)
    return candidate


def _kind_specificity(kind: str) -> int:
    normalized = _normalize_kind(kind)
    return 0 if normalized == "referenced_file" else 1
