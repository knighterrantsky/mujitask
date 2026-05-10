from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from automation_business_scaffold.contracts.handler.shared import (
    bundle_entity_keys,
    coerce_mapping,
    compact_dict,
    merge_fact_bundles,
)
from automation_business_scaffold.contracts.workflow import WorkflowDefinition
from automation_business_scaffold.contracts.workflow.execution_helpers import (
    all_child_records as _all_child_records,
    any_api_jobs_active as _any_api_jobs_active,
    any_browser_executions_active as _any_browser_executions_active,
    api_jobs_for_stage as _api_jobs_for_stage,
    browser_executions_for_stage as _browser_executions_for_stage,
    build_projection_record,
    build_projection_write_payload,
    build_stage_local_dedupe_key,
    compute_final_status,
    extract_effective_result_payload,
    extract_handler_result_status,
    has_active_records as _has_active_children,
    is_fallback_required,
    render_job_keys,
    select_latest_successful_api_job,
    select_latest_successful_api_job_result,
    stage_child_records as _stage_child_records,
    summarize_child_outcomes,
    summarize_stage_children,
    timeout_seconds_for_workflow as _timeout_seconds,
)
from automation_business_scaffold.domains.tiktok.mappers.keyword_search_mapper import (
    keyword_search_parameter_mapper,
)
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition
from .models import *


@dataclass(frozen=True)
class StageDecision:
    action: str
    stage_code: str
    next_stage: str = ""
    message: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_result(self) -> dict[str, Any]:
        if self.action == "waiting":
            return {
                "action": "waiting",
                "current_stage": self.stage_code,
                "message": self.message,
                "details": dict(self.details),
            }
        result: dict[str, Any] = {"action": self.action}
        if self.next_stage:
            result["next_stage"] = self.next_stage
        if self.details:
            result["details"] = dict(self.details)
        return result

def _finalize_fastmoss_security_required(
    import_job: Mapping[str, Any],
    *,
    details: Mapping[str, Any],
) -> dict[str, Any]:
    result_payload = extract_effective_result_payload(import_job)
    return {
        "action": "finalize",
        "final_status": "failed",
        "details": {
            "error_code": "fastmoss_security_verification_required",
            "fallback_required": True,
            "fallback_reason": "fastmoss_search_security_verification",
            "security_context": dict(result_payload.get("security_context") or {}),
            **dict(details),
        },
    }

def _waiting(*, stage_code: str, message: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "action": "waiting",
        "current_stage": stage_code,
        "message": message,
        "details": dict(details or {}),
    }

__all__ = [name for name in globals() if not name.startswith('__')]
