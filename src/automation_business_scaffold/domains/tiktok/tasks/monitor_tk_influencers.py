from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.contracts.workflow import RuntimeTaskShell
from automation_business_scaffold.control_plane.executor.runner import (
    run_monitor_tk_influencers_request,
)
from automation_business_scaffold.control_plane.runtime_config.settings import (
    INFLUENCER_MONITOR_TASK_CODE,
)
from automation_business_scaffold.domains.tiktok.policies.influencer_monitor_candidate_policy import (
    normalize_min_video_sales_28d,
)
from automation_business_scaffold.domains.tiktok.workflows import (
    build_monitor_tk_influencers_workflow,
)


TASK_CODE = INFLUENCER_MONITOR_TASK_CODE
SOURCE_TABLE_REF = "feishu://mujitask/tk_competitor"
TARGET_TABLE_REF = "feishu://mujitask/tk_influencer_monitoring"


class MonitorTKInfluencersTask(RuntimeTaskShell):
    name = TASK_CODE
    description = "Submit, inspect, or advance the independent TK influencer monitoring request."
    success_message = "Processed the TK influencer monitoring runtime request."

    def build_runtime_workflow(
        self,
        *,
        run_mode: str,
        control_action: str,
    ) -> WorkflowSpec:
        return build_monitor_tk_influencers_workflow(
            run_mode=run_mode,
            control_action=control_action,
        )

    def run_runtime_request(self, params: dict[str, object]) -> dict[str, object]:
        payload = dict(params)
        payload["min_video_sales_28d"] = normalize_min_video_sales_28d(
            payload.get("min_video_sales_28d")
        )
        payload.setdefault("source_table_ref", SOURCE_TABLE_REF)
        payload.setdefault("target_table_ref", TARGET_TABLE_REF)
        payload.setdefault("fastmoss_live_fetch", True)
        return run_monitor_tk_influencers_request(payload)


__all__ = [
    "MonitorTKInfluencersTask",
    "SOURCE_TABLE_REF",
    "TARGET_TABLE_REF",
    "TASK_CODE",
]
