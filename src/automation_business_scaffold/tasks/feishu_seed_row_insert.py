from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.flows import run_feishu_seed_row_insert
from automation_business_scaffold.workflows import build_feishu_seed_row_insert_workflow


class FeishuSeedRowInsertTask(BaseWorkflowTask):
    name = "feishu_seed_row_insert"
    description = (
        "Insert one new Feishu seed row for a discovered SKU and mark its source keyword in the remark field."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_feishu_seed_row_insert_workflow(run_mode=run_mode)

    def execute_workflow_step(self, context) -> FrameworkResult:
        if context.step.step_id != "insert_seed_row":
            raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
        payload = run_feishu_seed_row_insert(context.params)
        return FrameworkResult.ok(
            message="Inserted one Feishu seed row.",
            data=payload,
            metadata={"artifacts_payload": {"state_dump": payload}},
        )
