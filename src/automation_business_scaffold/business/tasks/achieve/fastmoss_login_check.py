from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.business.flows import run_fastmoss_login_check
from automation_business_scaffold.business.tasks.workflow_specs import build_single_step_workflow


class FastMossLoginCheckTask(BaseWorkflowTask):
    name = "fastmoss_login_check"
    description = "Validate the FastMoss account login once at the beginning of an orchestrated flow."

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_single_step_workflow(
            workflow_id="fastmoss_login_check_v1",
            run_mode=run_mode,
            step_id="validate_fastmoss_login",
            action_type="validate_fastmoss_login",
            postconditions=["result_data_exists:summary.total"],
            outputs=["summary", "item", "items"],
        )

    def execute_workflow_step(self, context) -> FrameworkResult:
        if context.step.step_id != "validate_fastmoss_login":
            raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
        payload = run_fastmoss_login_check(context.params)
        return FrameworkResult.ok(
            message="Validated FastMoss login.",
            data=payload,
            metadata={"artifacts_payload": {"state_dump": payload}},
        )
