from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.config import get_business_defaults
from automation_business_scaffold.flows import build_draft_form, build_publish_result
from automation_business_scaffold.mappers import map_source_item_to_publish_payload
from automation_business_scaffold.models import SourceItem
from automation_business_scaffold.validators import validate_publish_payload
from automation_business_scaffold.workflows import build_source_to_target_publish_workflow


class SourceToTargetPublishDemoTask(BaseWorkflowTask):
    name = "source_to_target_publish_demo"
    description = "Demo workflow showing extract -> map -> fill -> draft/submit on top of automation-framework."

    def build_workflow(self, params: dict[str, Any]):
        defaults = get_business_defaults()
        run_mode = str(params.get("run_mode", defaults.default_run_mode))
        include_submit = bool(params.get("include_submit", False))
        return build_source_to_target_publish_workflow(
            run_mode=run_mode,
            include_submit=include_submit,
        )

    def execute_workflow_step(self, context) -> FrameworkResult:
        defaults = get_business_defaults()
        trace_id = str(context.params.get("trace_id", "source_to_target_publish_demo"))

        if context.step.step_id == "extract_source_item":
            source_item = self._build_source_item(context.params, defaults)
            return FrameworkResult.ok(
                message="Extracted source item for demo business workflow.",
                data={"source_item": source_item.to_dict()},
                metadata={
                    "artifacts_payload": {
                        "state_dump": {
                            "trace_id": trace_id,
                            "source_item": source_item.to_dict(),
                            "step": context.step.step_id,
                        }
                    }
                },
            )

        if context.step.step_id == "map_publish_payload":
            previous = context.get_step_output("extract_source_item").get("source_item", {})
            source_item = SourceItem(
                title=str(previous.get("title", "")),
                price=int(previous.get("price", defaults.default_price)),
                category=str(previous.get("category", defaults.default_category)),
                description=str(previous.get("description", defaults.default_description)),
                source_url=str(previous.get("source_url", "")),
            )
            publish_payload = map_source_item_to_publish_payload(source_item, defaults)
            validate_publish_payload(publish_payload)
            return FrameworkResult.ok(
                message="Mapped source item into publish payload.",
                data={"publish_payload": publish_payload.to_dict()},
                metadata={
                    "artifacts_payload": {
                        "state_dump": {
                            "source_item": source_item.to_dict(),
                            "publish_payload": publish_payload.to_dict(),
                            "step": context.step.step_id,
                        }
                    }
                },
            )

        if context.step.step_id == "fill_target_form":
            payload = context.get_step_output("map_publish_payload").get("publish_payload", {})
            draft_form = build_draft_form(
                map_source_item_to_publish_payload(
                    SourceItem(
                        title=str(payload.get("title", "")),
                        price=int(payload.get("price", defaults.default_price)),
                        category=str(payload.get("category", defaults.default_category)),
                        description=str(payload.get("description", defaults.default_description)),
                        source_url=str(payload.get("source_url", "")),
                    ),
                    defaults,
                )
            )
            return FrameworkResult.ok(
                message="Filled target form for demo workflow.",
                data={"draft_form": draft_form},
                metadata={
                    "artifacts_payload": {
                        "state_dump": draft_form,
                        "html_snapshot": (
                            "<html><body>"
                            f"<h1>{draft_form['title']}</h1>"
                            f"<p>price={draft_form['price']}</p>"
                            f"<p>category={draft_form['category']}</p>"
                            "</body></html>"
                        ),
                    }
                },
            )

        if context.step.step_id == "save_target_draft":
            draft_form = context.get_step_output("fill_target_form").get("draft_form", {})
            draft_result = build_publish_result(
                trace_id=trace_id,
                draft_form=draft_form,
                submitted=False,
            )
            return FrameworkResult.ok(
                message="Saved target draft for demo workflow.",
                data={"draft_result": draft_result},
                metadata={"artifacts_payload": {"state_dump": draft_result}},
            )

        if context.step.step_id == "submit_target_publish":
            draft_form = context.get_step_output("fill_target_form").get("draft_form", {})
            publish_result = build_publish_result(
                trace_id=trace_id,
                draft_form=draft_form,
                submitted=True,
            )
            return FrameworkResult.ok(
                message="Submitted target publish for demo workflow.",
                data={"publish_result": publish_result},
                metadata={
                    "artifacts_payload": {
                        "state_dump": publish_result,
                        "html_snapshot": (
                            "<html><body>"
                            f"<h1>submitted:{publish_result['title']}</h1>"
                            "</body></html>"
                        ),
                    }
                },
            )

        raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")

    @staticmethod
    def _build_source_item(params: dict[str, Any], defaults) -> SourceItem:
        return SourceItem(
            title=str(params.get("title") or params.get("query") or "Demo Vintage Chair"),
            price=int(params.get("price", defaults.default_price)),
            category=str(params.get("category", defaults.default_category)),
            description=str(params.get("description", defaults.default_description)),
            source_url=str(params.get("source_url", "https://source.local/items/demo-001")),
        )

