from __future__ import annotations

from automation_business_scaffold.models import PublishPayload


def build_draft_form(payload: PublishPayload) -> dict[str, str | int]:
    return {
        "title": payload.title,
        "price": payload.price,
        "category": payload.category,
        "description": payload.description,
        "status": "filled",
    }


def build_publish_result(
    *,
    trace_id: str,
    draft_form: dict[str, str | int],
    submitted: bool,
) -> dict[str, str]:
    status = "submitted" if submitted else "saved"
    key = "publish_id" if submitted else "draft_id"
    suffix = "publish" if submitted else "draft"
    return {
        key: f"{trace_id}-{suffix}-001",
        "status": status,
        "title": str(draft_form.get("title", "")),
    }

