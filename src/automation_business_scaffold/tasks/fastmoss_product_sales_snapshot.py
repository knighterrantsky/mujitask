from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.flows import fetch_fastmoss_product_sales_via_browser
from automation_business_scaffold.workflows import build_fastmoss_product_sales_snapshot_workflow


class FastMossProductSalesSnapshotTask(BaseWorkflowTask):
    name = "fastmoss_product_sales_snapshot"
    description = (
        "Log into FastMoss if needed, search a product_id, open the detail page, and collect "
        "price plus yesterday/7d/28d/90d sales metrics."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_fastmoss_product_sales_snapshot_workflow(run_mode=run_mode)

    def execute_workflow_step(self, context) -> FrameworkResult:
        if context.step.step_id != "fetch_fastmoss_sales_snapshot":
            raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")

        trace_id = str(context.params.get("trace_id", self.name))
        snapshot = fetch_fastmoss_product_sales_via_browser(
            str(context.params.get("product_id") or context.params.get("sku_id") or "").strip(),
            profile_ref=str(context.params.get("profile_ref") or "").strip() or None,
            fastmoss_phone=str(context.params.get("fastmoss_phone") or "").strip() or None,
            fastmoss_password=str(context.params.get("fastmoss_password") or "").strip() or None,
            fastmoss_phone_env=str(context.params.get("fastmoss_phone_env") or "").strip() or None,
            fastmoss_password_env=str(context.params.get("fastmoss_password_env") or "").strip() or None,
            step_delay_sec=float(context.params.get("step_delay_sec", 2.0) or 2.0),
            login_settle_sec=float(context.params.get("login_settle_sec", 8.0) or 8.0),
            capture_detail_screenshot=_coerce_bool(context.params.get("capture_detail_screenshot"), default=True),
            verify_login=_coerce_bool(context.params.get("verify_fastmoss_login"), default=True),
        )
        snapshot_data = snapshot.to_dict()

        return FrameworkResult.ok(
            message="Fetched FastMoss detail price and stage-2 sales metrics.",
            data={"fastmoss_snapshot": snapshot_data},
            metadata={
                "artifacts_payload": {
                    "state_dump": {
                        "trace_id": trace_id,
                        "step": context.step.step_id,
                        "fastmoss_snapshot": snapshot_data,
                    }
                }
            },
        )


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Cannot parse boolean value from: {value}")
