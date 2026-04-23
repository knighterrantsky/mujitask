from __future__ import annotations

from typing import Any, Mapping

from automation_framework.core import BaseWorkflowTask, FrameworkResult

import automation_business_scaffold.business.flows.tiktok_fastmoss_product_ingest_flow as ingest_flow
import automation_business_scaffold.business.flows.refresh_current_competitor_table_flow as runtime_flow
from automation_business_scaffold.business.tasks.workflow_step_helpers import ok_result
from automation_business_scaffold.business.workflows import build_tiktok_fastmoss_product_ingest_workflow


class TikTokFastMossProductIngestTask(BaseWorkflowTask):
    name = "tiktok_fastmoss_product_ingest"
    description = (
        "Fetch one TikTok Shop product URL via Python requests, fetch FastMoss product API data by SKU, "
        "fall back to the browser loop when TikTok request HTML is not parseable, persist facts, "
        "and optionally bind/read/write the Feishu TK selection table through API worker jobs."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_tiktok_fastmoss_product_ingest_workflow(
            run_mode=run_mode,
            control_action=str(params.get("control_action", "run") or "run"),
        )

    def execute_workflow_step(self, context) -> FrameworkResult:
        step_id = str(context.step.step_id)

        if step_id == "orchestrate_tiktok_fastmoss_product_ingest":
            payload = runtime_flow.run_tiktok_fastmoss_product_ingest_request(context.params)
            return ok_result(payload, default_message="Processed the TikTok and FastMoss product ingest request.")

        if step_id == "fetch_tiktok_product_request":
            payload = ingest_flow.fetch_tiktok_product_via_request(context.params)
            return ok_result(payload, default_message="Fetched TikTok product details via Python requests.")

        if step_id == "fetch_fastmoss_product_api":
            tiktok_payload = context.get_step_output("fetch_tiktok_product_request")
            payload = ingest_flow.fetch_fastmoss_product_by_sku(
                context.params,
                product_id=_product_id_from_payload(context.params, tiktok_payload),
            )
            return ok_result(payload, default_message="Fetched FastMoss product API data by SKU.")

        if step_id == "upload_product_media_assets":
            payload = ingest_flow.upload_product_media_assets(
                context.params,
                tiktok_payload=context.get_step_output("fetch_tiktok_product_request"),
                fastmoss_payload=context.get_step_output("fetch_fastmoss_product_api"),
            )
            return ok_result(payload, default_message="Uploaded product media assets to MinIO.")

        if step_id == "persist_tiktok_fastmoss_product_facts":
            payload = ingest_flow.persist_tiktok_fastmoss_product_facts(
                context.params,
                tiktok_payload=context.get_step_output("fetch_tiktok_product_request"),
                fastmoss_payload=context.get_step_output("fetch_fastmoss_product_api"),
                media_upload_payload=context.get_step_output("upload_product_media_assets"),
            )
            return ok_result(payload, default_message="Persisted TikTok and FastMoss product facts.")

        raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")


def _product_id_from_payload(params: Mapping[str, Any], tiktok_payload: Mapping[str, Any]) -> str:
    item = tiktok_payload.get("item")
    item_payload = item if isinstance(item, Mapping) else {}
    for value in (
        params.get("sku_id"),
        params.get("product_id"),
        tiktok_payload.get("product_id"),
        item_payload.get("product_id"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""
