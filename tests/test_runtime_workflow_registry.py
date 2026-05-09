from __future__ import annotations

import importlib

import pytest

from automation_business_scaffold.control_plane.runtime_config.settings import (
    FORMAL_TASK_CODES,
    REFRESH_COMPETITOR_ROW_BY_URL_TASK_CODE,
    SELECTION_KEYWORD_TASK_CODE,
)
from automation_business_scaffold.control_plane.executor.runner import _resolve_workflow_runtime
from automation_business_scaffold.control_plane.executor.workflow_registry import (
    WORKFLOW_RUNTIME_MODULES,
    WorkflowRuntimeModule,
    load_workflow_runtime,
)
from automation_business_scaffold.domains.tiktok.workflows import (
    get_workflow_definition,
    list_workflow_definitions,
)


REQUIRED_RUNTIME_ENTRYPOINTS = tuple(
    name
    for name, value in vars(WorkflowRuntimeModule).items()
    if callable(value) and not name.startswith("_")
)


def test_workflow_registry_keys_match_formal_task_codes() -> None:
    assert set(WORKFLOW_RUNTIME_MODULES) == set(FORMAL_TASK_CODES)
    assert {workflow.task_code for workflow in list_workflow_definitions()} == set(FORMAL_TASK_CODES)


@pytest.mark.parametrize("task_code", FORMAL_TASK_CODES)
def test_formal_task_can_resolve_to_workflow_definition(task_code: str) -> None:
    workflow = get_workflow_definition(task_code)

    assert workflow.task_code == task_code
    assert workflow.workflow_code == task_code


@pytest.mark.parametrize("task_code", FORMAL_TASK_CODES)
def test_formal_runtime_module_path_can_import(task_code: str) -> None:
    module_name = WORKFLOW_RUNTIME_MODULES[task_code]

    module = importlib.import_module(module_name)

    for entrypoint in REQUIRED_RUNTIME_ENTRYPOINTS:
        assert hasattr(module, entrypoint), f"{task_code} runtime missing {entrypoint}"


@pytest.mark.parametrize("task_code", FORMAL_TASK_CODES)
def test_formal_runtime_modules_are_loadable_through_registry(task_code: str) -> None:
    runtime = load_workflow_runtime(task_code)

    assert runtime is not None
    for entrypoint in REQUIRED_RUNTIME_ENTRYPOINTS:
        assert hasattr(runtime, entrypoint), f"{task_code} runtime missing {entrypoint}"


@pytest.mark.parametrize(
    "task_code",
    (REFRESH_COMPETITOR_ROW_BY_URL_TASK_CODE, SELECTION_KEYWORD_TASK_CODE),
)
def test_newer_formal_tasks_are_not_left_out_of_runtime_registry(task_code: str) -> None:
    assert task_code in FORMAL_TASK_CODES
    assert task_code in WORKFLOW_RUNTIME_MODULES
    assert load_workflow_runtime(task_code) is not None


def test_unknown_runtime_module_returns_none() -> None:
    assert load_workflow_runtime("unknown_runtime_task") is None


@pytest.mark.parametrize("task_code", FORMAL_TASK_CODES)
def test_runner_runtime_facade_uses_the_same_formal_registry(task_code: str) -> None:
    runtime = _resolve_workflow_runtime(task_code)

    assert runtime is not None
    for entrypoint in REQUIRED_RUNTIME_ENTRYPOINTS:
        assert hasattr(runtime, entrypoint), f"{task_code} runtime missing {entrypoint}"
