#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys


PROJECT_PACKAGE = "automation_business_scaffold"
SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class ScaffoldRequest:
    repo_root: Path
    domain: str
    workflow_code: str
    task_code: str
    skill_code: str
    job_codes: tuple[str, ...]
    force: bool


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    request = ScaffoldRequest(
        repo_root=args.repo_root.resolve(),
        domain=args.domain,
        workflow_code=args.workflow_code,
        task_code=args.task_code or args.workflow_code,
        skill_code=args.skill_code,
        job_codes=tuple(args.job),
        force=args.force,
    )
    errors = _validate_request(request)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 2

    planned = _planned_files(request)
    existing = [path for path in planned if path.exists()]
    if existing and not request.force:
        print("Refusing to overwrite existing files:", file=sys.stderr)
        for path in existing:
            print(f"  {path.relative_to(request.repo_root)}", file=sys.stderr)
        print("Use --force only when intentionally regenerating scaffold files.", file=sys.stderr)
        return 1

    for path, content in planned.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(path.relative_to(request.repo_root))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create project-architecture scaffold files for a new workflow."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument("--domain", required=True, help="Business domain code, e.g. tiktok.")
    parser.add_argument("--workflow-code", required=True, help="Workflow code in snake_case.")
    parser.add_argument("--task-code", default="", help="Task code. Defaults to workflow-code.")
    parser.add_argument("--skill-code", required=True, help="Agent skill code owning the user entrypoint.")
    parser.add_argument(
        "--job",
        action="append",
        default=[],
        help="Job code to scaffold. Repeat for multiple jobs.",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing scaffold files.")
    return parser


def _validate_request(request: ScaffoldRequest) -> list[str]:
    values = {
        "domain": request.domain,
        "workflow_code": request.workflow_code,
        "task_code": request.task_code,
    }
    errors = [
        f"{name} must be snake_case: {value!r}"
        for name, value in values.items()
        if not SNAKE_CASE_RE.match(value)
    ]
    for job_code in request.job_codes:
        if not SNAKE_CASE_RE.match(job_code):
            errors.append(f"job code must be snake_case: {job_code!r}")
    if not request.job_codes:
        errors.append("at least one --job is required")
    return errors


def _planned_files(request: ScaffoldRequest) -> dict[Path, str]:
    package_root = request.repo_root / "src" / PROJECT_PACKAGE
    domain_root = package_root / "domains" / request.domain
    files: dict[Path, str] = {
        domain_root / "tasks" / f"{request.task_code}.py": _task_template(request),
        domain_root / "workflows" / f"{request.workflow_code}.py": _workflow_template(request),
        domain_root / "mappers" / f"{request.workflow_code}_mapper.py": _mapper_template(request),
        domain_root / "projections" / f"{request.workflow_code}_projection.py": _projection_template(request),
        domain_root / "policies" / f"{request.workflow_code}_policy.py": _policy_template(request),
        package_root / "contracts" / "workflow" / f"{request.workflow_code}.yaml": _manifest_template(request),
    }
    for job_code in request.job_codes:
        files[domain_root / "jobs" / f"{job_code}.py"] = _job_template(request, job_code)
    return files


def _class_name(code: str, suffix: str) -> str:
    return "".join(part.capitalize() for part in code.split("_")) + suffix


def _task_template(request: ScaffoldRequest) -> str:
    class_name = _class_name(request.task_code, "Task")
    build_workflow = f"build_{request.workflow_code}_workflow"
    return f'''from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.contracts.workflow import RuntimeTaskShell
from automation_business_scaffold.domains.{request.domain}.workflows.{request.workflow_code} import (
    {build_workflow},
)


TASK_CODE = "{request.task_code}"


class {class_name}(RuntimeTaskShell):
    name = TASK_CODE
    description = "Submit, inspect, or advance {request.workflow_code}."
    success_message = "Processed {request.workflow_code}."

    def build_runtime_workflow(
        self,
        *,
        run_mode: str,
        control_action: str,
    ) -> WorkflowSpec:
        return {build_workflow}(run_mode=run_mode, control_action=control_action)

    def run_runtime_request(self, params: dict[str, object]) -> dict[str, object]:
        raise NotImplementedError("Bind this task to control_plane/task_requests before enabling it.")


__all__ = ["TASK_CODE", "{class_name}"]
'''


def _workflow_template(request: ScaffoldRequest) -> str:
    builder = f"build_{request.workflow_code}_definition"
    runtime_builder = f"build_{request.workflow_code}_workflow"
    definition_name = f"{request.workflow_code.upper()}_DEFINITION"
    return f'''from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.contracts.workflow import (
    StageDefinition,
    StageJobBinding,
    SummaryPolicy,
    SummaryStatusRule,
    TransitionDefinition,
    WorkflowDefinition,
    build_formal_task_workflow,
    contract,
)


WORKFLOW_CODE = "{request.workflow_code}"


def {builder}() -> WorkflowDefinition:
    return WorkflowDefinition(
        task_code="{request.task_code}",
        workflow_code=WORKFLOW_CODE,
        contract_revision="draft",
        trigger_modes=("manual", "cli"),
        entry_stage_code="start",
        payload_contract=contract("{request.workflow_code}_payload"),
        stages=(
            StageDefinition(
                stage_code="start",
                description="Initial stage for {request.workflow_code}.",
                execution_mode="worker_jobs",
                enter_condition="task_request has been claimed",
                exit_condition="initial jobs are terminal",
                job_bindings=(
                    StageJobBinding(job_code="{request.job_codes[0]}", result_consumer="workflow result"),
                ),
            ),
        ),
        job_defs=(),
        transitions=(),
        summary_policy=SummaryPolicy(
            summary_stage_code="start",
            outbox_job_code="{request.job_codes[0]}",
            rules=(SummaryStatusRule(final_status="success", when="workflow completed"),),
        ),
    )


{definition_name} = {builder}()


def {runtime_builder}(
    *,
    run_mode: str = "draft",
    control_action: str = "submit",
) -> WorkflowSpec:
    del control_action
    return build_formal_task_workflow(workflow_code=WORKFLOW_CODE, run_mode=run_mode)


__all__ = [
    "WORKFLOW_CODE",
    "{definition_name}",
    "{builder}",
    "{runtime_builder}",
]
'''


def _job_template(request: ScaffoldRequest, job_code: str) -> str:
    const_name = f"{job_code.upper()}_JOB"
    return f'''from __future__ import annotations

from automation_business_scaffold.contracts.workflow import JobDefinition, contract


{const_name} = JobDefinition(
    job_code="{job_code}",
    handler_code="{job_code}",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="TODO: describe {job_code}.",
    payload_contract=contract("{job_code}_payload"),
    result_contract=contract("{job_code}_result"),
)


JOB_DEFINITION = {const_name}
JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = ["HANDLER_CODE", "JOB_CODE", "JOB_DEFINITION", "{const_name}"]
'''


def _mapper_template(request: ScaffoldRequest) -> str:
    function_name = f"{request.workflow_code}_mapper"
    return f'''from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def {function_name}(source: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    del payload
    return dict(source)


__all__ = ["{function_name}"]
'''


def _projection_template(request: ScaffoldRequest) -> str:
    function_name = f"{request.workflow_code}_projection"
    return f'''from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def {function_name}(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    del payload
    return {{"fields": dict(record)}}


__all__ = ["{function_name}"]
'''


def _policy_template(request: ScaffoldRequest) -> str:
    function_name = f"{request.workflow_code}_selection_policy"
    return f'''from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def {function_name}(candidate: Mapping[str, Any]) -> bool:
    return bool(candidate)


__all__ = ["{function_name}"]
'''


def _manifest_template(request: ScaffoldRequest) -> str:
    mapper_module = (
        f"automation_business_scaffold.domains.{request.domain}.mappers."
        f"{request.workflow_code}_mapper"
    )
    projection_module = (
        f"automation_business_scaffold.domains.{request.domain}.projections."
        f"{request.workflow_code}_projection"
    )
    policy_module = (
        f"automation_business_scaffold.domains.{request.domain}.policies."
        f"{request.workflow_code}_policy"
    )
    jobs = "\n".join(
        f"""  - code: {job_code}
    module: automation_business_scaffold.domains.{request.domain}.jobs.{job_code}
    handler_code: {job_code}
    capability:
      role: TODO
      system: TODO
      module: automation_business_scaffold.capabilities.TODO.{job_code}_handler
      export: {job_code}_handler"""
        for job_code in request.job_codes
    )
    task_class = _class_name(request.task_code, "Task")
    return f"""schema_version: 1
workflow_origin: new_workflow
workflow_code: {request.workflow_code}
domain: {request.domain}
agent_artifact:
  skill_code: {request.skill_code}
  path: skills/{request.skill_code}
  status: project_agent_artifact
task:
  code: {request.task_code}
  module: automation_business_scaffold.domains.{request.domain}.tasks.{request.task_code}
  exports:
    - {task_class}
workflow:
  module: automation_business_scaffold.domains.{request.domain}.workflows.{request.workflow_code}
  definition_export: build_{request.workflow_code}_definition
  runtime_export: build_{request.workflow_code}_workflow
  exports:
    - {request.workflow_code.upper()}_DEFINITION
custom_logic:
  mappers:
    - code: {request.workflow_code}_mapper
      module: {mapper_module}
      export: {request.workflow_code}_mapper
  policies:
    - code: {request.workflow_code}_selection_policy
      module: {policy_module}
      export: {request.workflow_code}_selection_policy
  projections:
    - code: {request.workflow_code}_projection
      module: {projection_module}
      export: {request.workflow_code}_projection
outbox:
  job_code: {request.job_codes[-1]}
  handler_code: {request.job_codes[-1]}
  capability:
    role: channel
    system: outbox
    module: automation_business_scaffold.capabilities.channels.outbox.message_dispatch_handler
    export: outbox_dispatch_handler
jobs:
{jobs}
known_architecture_gaps: []
"""


if __name__ == "__main__":
    raise SystemExit(main())
