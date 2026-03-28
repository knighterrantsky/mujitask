from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(slots=True)
class SourceItem:
    title: str
    price: int
    category: str
    description: str
    source_url: str

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


@dataclass(slots=True)
class PublishPayload:
    title: str
    price: int
    category: str
    description: str
    source_url: str
    source_system: str
    target_system: str

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)

