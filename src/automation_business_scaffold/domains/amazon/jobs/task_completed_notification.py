from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    JobDefinition,
    contract,
    optional_field,
    required_field,
)


TASK_COMPLETED_NOTIFICATION_JOB = JobDefinition(
    job_code="task_completed_notification",
    handler_code="outbox_dispatch",
    worker_type="outbox_dispatcher",
    runtime_table="notification_outbox",
    purpose="Deliver the finalized Amazon product row summary through the outbox.",
    payload_contract=contract(
        "amazon_task_completed_notification_payload",
        required_field("request_id", "Top-level task request id.", type_hint="str"),
        required_field(
            "summary_payload",
            "Compact Amazon row summary.",
            type_hint="dict[str, Any]",
        ),
        optional_field("reply_target", "Outbound reply target.", type_hint="str"),
    ),
    result_contract=contract(
        "amazon_task_completed_notification_result",
        required_field("event_type", "Dispatched outbox event type.", type_hint="str"),
    ),
    business_key_template="{request_id}",
    dedupe_key_template="task_request.completed:{request_id}",
    side_effects=("notification_outbox",),
)


JOB_DEFINITION = TASK_COMPLETED_NOTIFICATION_JOB
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = [
    "HANDLER_CODE",
    "JOB_CODE",
    "JOB_DEFINITION",
    "TASK_COMPLETED_NOTIFICATION_JOB",
]
