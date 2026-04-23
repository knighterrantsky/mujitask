from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.business.flows import run_fastmoss_keyword_candidate_discovery
from automation_business_scaffold.business.tasks.workflow_specs import build_single_step_workflow


class FastMossKeywordCandidateDiscoveryTask(BaseWorkflowTask):
    name = "fastmoss_keyword_candidate_discovery"
    description = (
        "Search FastMoss by keyword, keep products whose 7-day sales exceed the threshold, and skip existing Feishu items."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_single_step_workflow(
            workflow_id="fastmoss_keyword_candidate_discovery_v1",
            run_mode=run_mode,
            step_id="discover_keyword_candidates",
            action_type="discover_keyword_candidates",
            postconditions=["result_data_exists:summary.total"],
            outputs=["summary", "items", "target_items", "settings"],
        )

    def execute_workflow_step(self, context) -> FrameworkResult:
        if context.step.step_id != "discover_keyword_candidates":
            raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
        payload = run_fastmoss_keyword_candidate_discovery(context.params)
        return FrameworkResult.ok(
            message="Discovered FastMoss keyword candidates.",
            data=payload,
            metadata={"artifacts_payload": {"state_dump": payload}},
        )
