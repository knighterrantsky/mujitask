# Public Capability Status

这份文档只回答一个问题：

**业务仓库今天到底可以依赖什么能力，哪些能力还不能当成正式 contract。**

当前基线：

- framework version: `0.2.1`
- reference commit: `55e8223a92f562f4053006c55e66fe5491c9be61`

状态枚举：

- `ga`: 可以作为业务默认依赖面
- `beta`: 可以试点使用，但升级时需要先看迁移文档
- `experimental`: 只适合平台联调，不建议业务正式依赖
- `planned`: 已进入路线，但当前还不能依赖

## 能力矩阵

| capability_id | status | business_ready | since_version | 说明 |
| --- | --- | --- | --- | --- |
| `agent.create_app` | `ga` | yes | `0.2.1` | 业务仓库通过显式注册 task 接入 framework |
| `task_registry` | `ga` | yes | `0.2.1` | `TaskRegistry` 可稳定用于业务 task 注册 |
| `base_workflow_task` | `ga` | yes | `0.2.1` | 新业务默认应基于 `BaseWorkflowTask` 开发 |
| `workflow_spec_runtime` | `ga` | yes | `0.2.1` | `WorkflowSpec` / `StepDefinition` / `StepAction` 可直接用于业务 workflow 接入 |
| `run_step_signal_artifact_api` | `ga` | yes | `0.2.1` | `GET /runs/{run_id}/steps`、`signals`、`artifacts` 已可用 |
| `run_mode_effect_enforcement` | `ga` | yes | `0.2.1` | runtime 会阻止 `draft` 模式下的 `submit` effect |
| `manual_recording_api` | `beta` | yes | `0.2.1` | 录制、review、artifact 查询可用，但仍属 Recorder MVP |
| `workflow_draft_generation` | `beta` | yes | `0.2.1` | 可生成 review-only `workflow_draft`，不可直接执行 |
| `workflow_draft_contract` | `beta` | yes | `0.2.1` | 适合人工审核与业务整理，不是 runtime executable contract |
| `workflow_yaml_loader` | `planned` | no | n/a | runtime 还不能直接加载业务 `workflow.yaml` |
| `replay_api` | `planned` | no | n/a | 还没有公开 replay 接口 |
| `llm_repair_loop` | `planned` | no | n/a | 还没有公开 repair / ReAct 运行时 |

## 业务现在可以直接做什么

- 在独立业务仓库中显式注册 task，并启动自己的 agent
- 使用 `BaseWorkflowTask` 把业务流程切分为 step
- 在 step 上声明 `effects`、`preconditions`、`postconditions`
- 通过 `/runs/{run_id}/steps|signals|artifacts` 做排障与回放分析
- 用 recorder 产出 `raw_trace`、review summary 和 review-only `workflow_draft`

## 业务现在不要依赖什么

- 不要依赖 runtime 直接加载 `workflow.yaml`
- 不要把 `workflow_draft` 当成可执行 workflow
- 不要依赖 browser/provider 的内部实现路径
- 不要把 selftest task 当作业务 contract

## 支撑文档

- 业务接入边界见 [business-consumption-contract.md](./business-consumption-contract.md)
- 公开 import 面见 [public-import-surface.md](./public-import-surface.md)
- workflow step contract 见 [workflow-runtime-contract.md](./workflow-runtime-contract.md)
- review-only draft contract 见 [workflow-draft-contract.md](./workflow-draft-contract.md)
