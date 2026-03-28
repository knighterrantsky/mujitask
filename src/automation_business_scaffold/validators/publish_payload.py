from __future__ import annotations

from automation_business_scaffold.models import PublishPayload


def validate_publish_payload(payload: PublishPayload) -> None:
    if not payload.title.strip():
        raise ValueError("publish payload title is required")
    if int(payload.price) <= 0:
        raise ValueError("publish payload price must be greater than zero")

