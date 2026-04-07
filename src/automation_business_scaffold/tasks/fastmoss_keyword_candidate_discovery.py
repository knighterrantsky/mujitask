from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.flows import run_fastmoss_keyword_candidate_discovery
from automation_business_scaffold.workflows import build_fastmoss_keyword_candidate_discovery_workflow


class FastMossKeywordCandidateDiscoveryTask(BaseWorkflowTask):
    name = "fastmoss_keyword_candidate_discovery"
    description = (
        "Search FastMoss by keyword, keep products whose 7-day sales exceed the threshold, and skip existing Feishu items."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_fastmoss_keyword_candidate_discovery_workflow(run_mode=run_mode)

    def execute_workflow_step(self, context) -> FrameworkResult:
        if context.step.step_id != "discover_keyword_candidates":
            raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
        payload = run_fastmoss_keyword_candidate_discovery(context.params)
        return FrameworkResult.ok(
            message="Discovered FastMoss keyword candidates.",
            data=payload,
            metadata={"artifacts_payload": {"state_dump": payload}},
        )
