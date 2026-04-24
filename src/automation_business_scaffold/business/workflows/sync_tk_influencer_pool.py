from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from .runtime_workflow_shell import build_formal_task_workflow


def build_sync_tk_influencer_pool_workflow(
    *,
    run_mode: str = "draft",
    control_action: str = "submit",
) -> WorkflowSpec:
    del control_action
    return build_formal_task_workflow(
        workflow_code="sync_tk_influencer_pool",
        run_mode=run_mode,
    )
