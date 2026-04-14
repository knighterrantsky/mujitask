from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ArtifactObjectRecord:
    artifact_id: str
    run_id: str
    step_id: str
    kind: str
    bucket: str
    object_key: str
    etag: str
    size: int
    content_type: str
    source_path: str
    created_at: float
    request_id: str = ""
    execution_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
