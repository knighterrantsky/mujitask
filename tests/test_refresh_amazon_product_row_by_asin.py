from __future__ import annotations

from automation_business_scaffold.control_plane.executor.workflow_registry import (
    get_workflow_definition as get_registered_workflow_definition,
    load_workflow_runtime,
)
from automation_business_scaffold.control_plane.runtime_config.settings import (
    AMAZON_PRODUCT_ROW_TASK_CODE,
    FORMAL_TASK_CODES,
)
from automation_business_scaffold.domains.amazon.tasks.refresh_amazon_product_row_by_asin import (
    RefreshAmazonProductRowByAsinTask,
)
from automation_business_scaffold.domains.amazon.workflows import (
    get_workflow_definition,
)
from automation_business_scaffold.domains.amazon.workflows.refresh_amazon_product_row_by_asin import (
    REFRESH_AMAZON_PRODUCT_ROW_BY_ASIN_DEFINITION,
)


TASK_CODE = "refresh_amazon_product_row_by_asin"
EXPECTED_STAGES = (
    "read_amazon_product_row",
    "collect_amazon_product_detail",
    "persist_amazon_product_detail",
    "ready_for_summary",
)


def test_amazon_single_product_task_uses_the_formal_runtime_shell() -> None:
    task = RefreshAmazonProductRowByAsinTask()

    workflow = task.build_workflow({})

    assert task.name == TASK_CODE
    assert workflow.workflow_id == TASK_CODE
    assert workflow.run_mode == "full_auto"
    assert [step.step_id for step in workflow.steps] == ["dispatch_task_request"]
    assert workflow.steps[0].action.type == "dispatch_task_request"


def test_amazon_single_product_workflow_has_exact_four_stage_contract() -> None:
    definition = REFRESH_AMAZON_PRODUCT_ROW_BY_ASIN_DEFINITION

    assert definition.task_code == TASK_CODE
    assert definition.workflow_code == TASK_CODE
    assert definition.entry_stage_code == EXPECTED_STAGES[0]
    assert definition.stage_codes == EXPECTED_STAGES
    assert definition.payload_contract.field_names(required_only=True) == (
        "table_ref",
        "source_record_id",
    )
    assert definition.payload_contract.field_names() == ("table_ref", "source_record_id")
    assert [transition.from_stage_code for transition in definition.transitions] == list(
        EXPECTED_STAGES[:-1]
    )
    assert [transition.to_stage_code for transition in definition.transitions] == list(
        EXPECTED_STAGES[1:]
    )

    read_stage, browser_stage, persist_stage, summary_stage = definition.stages
    assert read_stage.execution_mode == "worker_jobs"
    assert read_stage.job_codes == ("feishu_table_read", "feishu_table_write")
    assert read_stage.job_bindings[0].adapter_code == "amazon_product_table_source_adapter"
    assert read_stage.job_bindings[1].optional is True
    assert browser_stage.execution_mode == "worker_jobs"
    assert browser_stage.job_codes == ("feishu_table_write", "amazon_product_browser_fetch")
    assert browser_stage.job_bindings[0].optional is True
    assert "fallback" not in browser_stage.stage_code
    assert persist_stage.execution_mode == "worker_jobs"
    assert persist_stage.job_codes == ("feishu_table_write", "amazon_product_row_persist")
    assert persist_stage.job_bindings[0].optional is True
    assert summary_stage.execution_mode == "summary"
    assert summary_stage.job_codes == ("task_completed_notification",)

    assert definition.require_job("feishu_table_read").runtime_table == "api_worker_job"
    assert definition.require_job("feishu_table_write").handler_code == "feishu_table_write"
    assert definition.require_job("feishu_table_write").runtime_table == "api_worker_job"
    assert definition.require_job("amazon_product_browser_fetch").runtime_table == "task_execution"
    assert definition.require_job("amazon_product_browser_fetch").worker_type == "browser_worker"
    assert definition.require_job("amazon_product_row_persist").runtime_table == "api_worker_job"
    assert definition.require_job("task_completed_notification").runtime_table == (
        "notification_outbox"
    )


def test_amazon_workflow_is_available_through_domain_and_control_plane_registries() -> None:
    assert AMAZON_PRODUCT_ROW_TASK_CODE == TASK_CODE
    assert TASK_CODE in FORMAL_TASK_CODES
    assert get_workflow_definition(TASK_CODE) is REFRESH_AMAZON_PRODUCT_ROW_BY_ASIN_DEFINITION
    assert (
        get_registered_workflow_definition(TASK_CODE)
        is REFRESH_AMAZON_PRODUCT_ROW_BY_ASIN_DEFINITION
    )
    assert load_workflow_runtime(TASK_CODE) is not None
