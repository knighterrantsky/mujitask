from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

WorkerType = Literal["api_worker", "browser_worker", "outbox_dispatcher"]
RuntimeTable = Literal["task_request", "api_worker_job", "task_execution", "notification_outbox"]
StageExecutionMode = Literal["worker_jobs", "executor_action", "summary"]
TransitionType = Literal["automatic", "conditional"]
FinalStatus = Literal["success", "partial_success", "failed"]
TerminalJobStatus = Literal["success", "skipped", "partial_success", "failed"]

DEFAULT_TERMINAL_JOB_STATUSES: tuple[TerminalJobStatus, ...] = (
    "success",
    "skipped",
    "partial_success",
    "failed",
)


@dataclass(frozen=True, slots=True)
class ContractField:
    name: str
    description: str
    type_hint: str = "Any"
    required: bool = True


@dataclass(frozen=True, slots=True)
class ContractDefinition:
    name: str
    fields: tuple[ContractField, ...] = ()
    notes: tuple[str, ...] = ()

    def field_names(self, *, required_only: bool = False) -> tuple[str, ...]:
        if not required_only:
            return tuple(field.name for field in self.fields)
        return tuple(field.name for field in self.fields if field.required)

    def get_field(self, name: str) -> ContractField | None:
        for field_def in self.fields:
            if field_def.name == name:
                return field_def
        return None


EMPTY_CONTRACT = ContractDefinition(name="empty")


def required_field(name: str, description: str, *, type_hint: str = "Any") -> ContractField:
    return ContractField(name=name, description=description, type_hint=type_hint, required=True)


def optional_field(name: str, description: str, *, type_hint: str = "Any") -> ContractField:
    return ContractField(name=name, description=description, type_hint=type_hint, required=False)


def contract(
    name: str,
    *fields: ContractField,
    notes: Iterable[str] = (),
) -> ContractDefinition:
    return ContractDefinition(name=name, fields=tuple(fields), notes=tuple(notes))


@dataclass(frozen=True, slots=True)
class JobDefinition:
    job_code: str
    handler_code: str
    worker_type: WorkerType
    runtime_table: RuntimeTable
    purpose: str
    payload_contract: ContractDefinition = EMPTY_CONTRACT
    result_contract: ContractDefinition = EMPTY_CONTRACT
    business_key_template: str = ""
    dedupe_key_template: str = ""
    side_effects: tuple[str, ...] = ()
    terminal_statuses: tuple[TerminalJobStatus, ...] = DEFAULT_TERMINAL_JOB_STATUSES
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StageJobBinding:
    job_code: str
    adapter_code: str = ""
    mapper_code: str = ""
    flow_code: str = ""
    detail_level: str = ""
    result_consumer: str = ""
    optional: bool = False
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedStageJobDefinition:
    stage_code: str
    job_code: str
    handler_code: str
    worker_type: WorkerType
    runtime_table: RuntimeTable
    purpose: str
    payload_contract: ContractDefinition
    result_contract: ContractDefinition
    business_key_template: str
    dedupe_key_template: str
    side_effects: tuple[str, ...]
    terminal_statuses: tuple[TerminalJobStatus, ...]
    adapter_code: str = ""
    mapper_code: str = ""
    flow_code: str = ""
    detail_level: str = ""
    result_consumer: str = ""
    optional: bool = False
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StageDefinition:
    stage_code: str
    description: str
    execution_mode: StageExecutionMode
    enter_condition: str
    exit_condition: str
    job_bindings: tuple[StageJobBinding, ...] = ()
    executor_action_code: str = ""
    notes: tuple[str, ...] = ()

    @property
    def job_codes(self) -> tuple[str, ...]:
        return tuple(binding.job_code for binding in self.job_bindings)


@dataclass(frozen=True, slots=True)
class TransitionDefinition:
    from_stage_code: str
    to_stage_code: str
    condition: str
    transition_type: TransitionType = "automatic"


@dataclass(frozen=True, slots=True)
class SummaryStatusRule:
    final_status: FinalStatus
    when: str


@dataclass(frozen=True, slots=True)
class SummaryPolicy:
    summary_stage_code: str
    outbox_job_code: str
    rules: tuple[SummaryStatusRule, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IdempotencyRule:
    scope: str
    key_template: str
    description: str


@dataclass(frozen=True, slots=True)
class TimeoutRule:
    target_code: str
    timeout_seconds: int
    description: str


@dataclass(frozen=True, slots=True)
class WatchdogRule:
    rule_code: str
    condition: str
    action: str


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    task_code: str
    workflow_code: str
    entry_stage_code: str
    stages: tuple[StageDefinition, ...]
    job_defs: tuple[JobDefinition, ...]
    transitions: tuple[TransitionDefinition, ...]
    summary_policy: SummaryPolicy
    idempotency_policy: tuple[IdempotencyRule, ...] = ()
    timeout_policy: tuple[TimeoutRule, ...] = ()
    watchdog_policy: tuple[WatchdogRule, ...] = ()
    contract_revision: str = ""
    trigger_modes: tuple[str, ...] = ("manual",)
    payload_contract: ContractDefinition = EMPTY_CONTRACT
    summary_contract: ContractDefinition = EMPTY_CONTRACT
    error_contract: ContractDefinition = EMPTY_CONTRACT
    notes: tuple[str, ...] = ()
    _stage_index: dict[str, StageDefinition] = field(init=False, repr=False, compare=False)
    _job_index: dict[str, JobDefinition] = field(init=False, repr=False, compare=False)
    _transition_index: dict[str, tuple[TransitionDefinition, ...]] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        stage_index: dict[str, StageDefinition] = {}
        for stage in self.stages:
            if stage.stage_code in stage_index:
                raise ValueError(f"Duplicate stage_code in workflow {self.workflow_code}: {stage.stage_code}")
            stage_index[stage.stage_code] = stage

        if self.entry_stage_code not in stage_index:
            raise ValueError(
                f"Workflow {self.workflow_code} entry_stage_code {self.entry_stage_code!r} is not defined"
            )

        job_index: dict[str, JobDefinition] = {}
        for job_def in self.job_defs:
            if job_def.job_code in job_index:
                raise ValueError(f"Duplicate job_code in workflow {self.workflow_code}: {job_def.job_code}")
            job_index[job_def.job_code] = job_def

        transition_index: dict[str, list[TransitionDefinition]] = {}
        for transition in self.transitions:
            if transition.from_stage_code not in stage_index:
                raise ValueError(
                    f"Workflow {self.workflow_code} transition references unknown stage "
                    f"{transition.from_stage_code!r}"
                )
            if transition.to_stage_code not in stage_index:
                raise ValueError(
                    f"Workflow {self.workflow_code} transition references unknown stage "
                    f"{transition.to_stage_code!r}"
                )
            transition_index.setdefault(transition.from_stage_code, []).append(transition)

        for stage in self.stages:
            for binding in stage.job_bindings:
                if binding.job_code not in job_index:
                    raise ValueError(
                        f"Workflow {self.workflow_code} stage {stage.stage_code!r} references unknown "
                        f"job_code {binding.job_code!r}"
                    )

        if self.summary_policy.summary_stage_code not in stage_index:
            raise ValueError(
                f"Workflow {self.workflow_code} summary stage {self.summary_policy.summary_stage_code!r} "
                "is not defined"
            )
        if self.summary_policy.outbox_job_code not in job_index:
            raise ValueError(
                f"Workflow {self.workflow_code} summary outbox job "
                f"{self.summary_policy.outbox_job_code!r} is not defined"
            )

        object.__setattr__(self, "_stage_index", stage_index)
        object.__setattr__(self, "_job_index", job_index)
        object.__setattr__(
            self,
            "_transition_index",
            {stage_code: tuple(items) for stage_code, items in transition_index.items()},
        )

    @property
    def stage_codes(self) -> tuple[str, ...]:
        return tuple(self._stage_index.keys())

    @property
    def job_codes(self) -> tuple[str, ...]:
        return tuple(self._job_index.keys())

    def get_stage(self, stage_code: str) -> StageDefinition | None:
        return self._stage_index.get(stage_code)

    def require_stage(self, stage_code: str) -> StageDefinition:
        stage = self.get_stage(stage_code)
        if stage is None:
            raise KeyError(f"Unknown stage_code for workflow {self.workflow_code}: {stage_code}")
        return stage

    def get_job(self, job_code: str) -> JobDefinition | None:
        return self._job_index.get(job_code)

    def require_job(self, job_code: str) -> JobDefinition:
        job_def = self.get_job(job_code)
        if job_def is None:
            raise KeyError(f"Unknown job_code for workflow {self.workflow_code}: {job_code}")
        return job_def

    def transitions_from(self, stage_code: str) -> tuple[TransitionDefinition, ...]:
        self.require_stage(stage_code)
        return self._transition_index.get(stage_code, ())

    def next_stage_codes(self, stage_code: str) -> tuple[str, ...]:
        return tuple(transition.to_stage_code for transition in self.transitions_from(stage_code))

    def resolve_stage_jobs(self, stage_code: str) -> tuple[ResolvedStageJobDefinition, ...]:
        stage = self.require_stage(stage_code)
        resolved: list[ResolvedStageJobDefinition] = []
        for binding in stage.job_bindings:
            job_def = self.require_job(binding.job_code)
            binding_notes = tuple(note for note in binding.notes if note)
            resolved.append(
                ResolvedStageJobDefinition(
                    stage_code=stage.stage_code,
                    job_code=job_def.job_code,
                    handler_code=job_def.handler_code,
                    worker_type=job_def.worker_type,
                    runtime_table=job_def.runtime_table,
                    purpose=job_def.purpose,
                    payload_contract=job_def.payload_contract,
                    result_contract=job_def.result_contract,
                    business_key_template=job_def.business_key_template,
                    dedupe_key_template=job_def.dedupe_key_template,
                    side_effects=job_def.side_effects,
                    terminal_statuses=job_def.terminal_statuses,
                    adapter_code=binding.adapter_code,
                    mapper_code=binding.mapper_code,
                    flow_code=binding.flow_code,
                    detail_level=binding.detail_level,
                    result_consumer=binding.result_consumer,
                    optional=binding.optional,
                    notes=job_def.notes + binding_notes,
                )
            )
        return tuple(resolved)
