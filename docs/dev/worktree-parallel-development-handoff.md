# Worktree 并行开发交接说明

更新时间: 2026-04-24

状态: 当前 checkpoint 之后的下一轮并行开发手册

## 1. 目的

本文档用于指导当前 checkpoint 之后的下一轮 worktree 并行开发，确保每个窗口都有明确的写入边界、验证范围和交接标准。

当前统一基线:

- 基线提交: `657ce68`
- 提交说明: `checkpoint: land modular runtime rewrite baseline`
- 主分支工作区: `codex/workflow-redesign-docs`

本轮目标不是继续扩散架构讨论，而是把已经落下来的 runtime / supervisor / watchdog / modular workflow 基座继续往真实运行链路推进。

## 2. 通用规则

所有 worktree 窗口都遵守以下约束:

1. 开工前先确认自己所在路径和分支:
   - `git status --short --branch`
2. 旧实现只允许参考 `achieve/`，禁止从运行时代码 import `achieve/`。
3. 尽量只修改自己负责的文件集合；如确实需要跨边界改动，先把改动缩到最小，并在提交说明里写清原因。
4. 本轮不做新的业务抽象层扩散，不引入新的领域专用 handler / job family 命名。
5. 所有实现都优先复用当前 `handler registry`、`workflow_defs`、`runtime_store`、`execution_supervisor` 契约。
6. 每条线都需要带上对应测试；没有测试支撑的“感觉正确”不作为可合并结果。
7. 除非任务明确需要，不要顺手改 `docs/arch`。当前窗口主要做代码落地。

## 3. 开窗口前统一检查

每个窗口建议先执行:

```bash
git status --short --branch
uv run pytest -q tests/test_registry.py tests/test_workflow_defs_contract.py tests/test_handler_registry_contract.py
```

如果窗口任务涉及 runtime / worker / outbox / watchdog，再追加对应专项测试，而不是一上来全量跑全仓库。

## 4. 首条提示词之后的开发循环

每个 worktree 的首条提示词只是启动上下文。提示词完成之后，该窗口进入独立开发循环，目标是把本条线做到可提交、可合并、可验证。

标准流程:

1. 读上下文
   - 读取本文档对应 worktree 小节。
   - 读取负责范围内的源码和测试。
   - 确认本窗口不改其他 worktree 的负责范围。
2. 给出本窗口小计划
   - 列出 3 到 5 个具体任务。
   - 标出预计修改的文件。
   - 标出准备运行的测试。
3. 直接开发
   - 写代码和测试。
   - 旧实现只能参考 `achieve/`，不能从 runtime import。
   - 如发现必须跨边界修改，先说明原因，并把改动压到最小。
4. 本地验证
   - 运行本小节列出的专项测试。
   - 运行 `git diff --check`。
   - 如改了公共 contract，再补跑 contract tests。
5. 提交到本 worktree 分支
   - 每条线完成一个可合并单元后，在自己的 worktree commit。
   - 不要从 worktree 自己合并回主工作区。
6. 向主窗口回报
   - 回报 commit id。
   - 回报改了哪些文件。
   - 回报跑过哪些测试和结果。
   - 回报是否有合并风险、跨线依赖或未完成事项。

主窗口只负责汇总、集成和最终回归。三个 worktree 的开发成果先停留在各自工作目录和各自分支里。

### 4.1 全局可合并停止信号

本轮并行开发里的“主分支”先指重构集成分支 `codex/workflow-redesign-docs`。三个 worktree 不直接合并到 `main`。

每个 worktree 满足以下条件时，就应该停止继续扩展范围，提交并向主窗口回报:

1. 本 worktree 的完成标准已经满足。
2. 负责范围内的专项测试通过。
3. `git diff --check` 通过。
4. 代码没有从 `achieve/` import。
5. 没有引入新的领域专用 handler / job family 命名。
6. 没有修改其他 worktree 的主要负责范围；如确实修改了，已在回报中说明原因和影响。
7. 已经在本 worktree 分支提交 commit。

停止信号不是“所有能优化的地方都优化完”。停止信号是“本条线的最小可合并目标已经完成，并且后续工作可以作为下一轮任务独立排期”。

### 4.2 必须暂停并回报的信号

遇到以下情况时，不要继续扩大改动，应暂停并向主窗口回报:

1. 需要修改另一个 worktree 的核心文件才能继续。
2. 需要改变 `handler registry`、`workflow_defs`、`runtime_store` 或 `execution_supervisor` 的公共 contract。
3. 专项测试失败，但失败点看起来不属于本 worktree 的改动范围。
4. 需要新增 Runtime DB 字段或 migration。
5. 发现当前 handoff 目标和真实代码冲突。
6. 为了通过测试需要回退或删除别人已经提交的改动。

### 4.3 回报格式

每个 worktree 完成后，向主窗口回报以下内容:

```text
worktree:
branch:
commit:
changed files:
tests:
merge readiness:
risks / follow-ups:
```

## 5. Worktree A: workflow-common-helper

### 5.1 基本信息

- 分支: `codex/workflow-common-helper`
- 路径: `/Users/happyzhao/Work/mujitask-wt-workflow-common-helper`

### 5.2 负责范围

优先负责以下文件:

- `src/automation_business_scaffold/business/workflow_defs/execution_helpers.py`
- `src/automation_business_scaffold/business/flows/runtime_refresh_current_competitor_table.py`
- `src/automation_business_scaffold/business/flows/runtime_search_keyword_competitor_products.py`
- 相关测试:
  - `tests/test_runtime_refresh_current_competitor_table.py`
  - `tests/test_runtime_search_keyword_competitor_products.py`
  - `tests/test_runtime_refresh_executor_integration.py`
  - `tests/test_runtime_keyword_executor_integration.py`

### 5.3 主要目标

这一条线继续做 `refresh` / `keyword` 的共享逻辑收敛，但前提是行为不变。

建议优先处理:

1. stage-local dedupe key / business key 生成逻辑中重复的部分
2. row projection / writeback payload 组装中重复的部分
3. browser fallback 之后 continuation resume 的重复判断
4. refresh / keyword 之间仅参数不同、流程相同的小型 helper

### 5.4 完成标准

- `refresh` / `keyword` 的重复逻辑被收敛到共享 helper。
- 两个 workflow 的现有行为保持一致。
- 没有修改 outbox / watchdog / runtime store 负责范围。
- 对应专项测试通过。

推荐停止点:

- 至少完成一类真实重复逻辑的收敛，例如 stage-local key 生成、projection/writeback payload 组装或 browser fallback continuation 判断。
- 两个 workflow 的专项测试仍然通过。
- 如果扫描出更多可优化点，但会扩大到新的业务抽象或更多 workflow，应记录为 follow-up，不继续在本轮展开。

可合并信号:

- 可以清楚说明“抽出了哪些 helper，refresh / keyword 哪些重复逻辑被删除，行为为什么保持一致”。
- `tests/test_runtime_refresh_current_competitor_table.py`、`tests/test_runtime_search_keyword_competitor_products.py`、两个 executor integration 测试通过。

### 5.5 不要做的事

- 不要改 `outbox_dispatcher`
- 不要改 `watchdog_scanner`
- 不要改 `runtime_store`
- 不要把共享 helper 继续抽成新的业务层级

### 5.6 建议验证

```bash
uv run pytest -q \
  tests/test_runtime_refresh_current_competitor_table.py \
  tests/test_runtime_search_keyword_competitor_products.py \
  tests/test_runtime_refresh_executor_integration.py \
  tests/test_runtime_keyword_executor_integration.py
```

收口前建议再补跑:

```bash
uv run pytest -q \
  tests/test_runtime_workflow_registry.py \
  tests/test_runtime_refresh_current_competitor_table.py \
  tests/test_runtime_search_keyword_competitor_products.py \
  tests/test_runtime_refresh_executor_integration.py \
  tests/test_runtime_keyword_executor_integration.py
```

### 5.7 窗口首条提示词

```text
你现在在 /Users/happyzhao/Work/mujitask-wt-workflow-common-helper 上工作。先阅读 docs/dev/worktree-parallel-development-handoff.md 的 Worktree A 部分，只做 refresh / keyword 的共享 helper 收敛，不要改 outbox/watchdog/runtime_store。先扫描 execution_helpers.py、runtime_refresh_current_competitor_table.py、runtime_search_keyword_competitor_products.py 里仍然重复的 stage-local dedupe / projection 逻辑，然后给出最小实现方案并直接开始改代码，最后跑文档里列出的测试。
```

## 6. Worktree B: outbox-e2e

### 6.1 基本信息

- 分支: `codex/outbox-e2e`
- 路径: `/Users/happyzhao/Work/mujitask-wt-outbox-e2e`

### 6.2 负责范围

优先负责以下文件:

- `src/automation_business_scaffold/outbox_dispatcher.py`
- `src/automation_business_scaffold/business/handlers/outbox/__init__.py`
- `src/automation_business_scaffold/business/handlers/outbox/registry.py`
- `src/automation_business_scaffold/business/handlers/outbox/implementations.py`
- 如确有必要，可触达:
  - `src/automation_business_scaffold/business/flows/runtime_orchestrator.py`
  - `src/automation_business_scaffold/business/flows/execution_supervisor.py`
- 相关测试:
  - `tests/test_execution_supervisor_runtime.py`
  - `tests/test_runtime_lifecycle.py`
  - `tests/test_runtime_store.py`
  - 可新增 outbox integration tests

### 6.3 主要目标

这一条线的目标是把 outbox 从“能跑”推进到“端到端闭环可验证”。

建议优先处理:

1. `notification_outbox` claim -> supervisor -> `outbox_dispatch` handler -> `sent/retry/failed`
2. `noop` / `console` / 统一 channel dispatch 的稳定路径
3. 失败分类、progress 更新、lease reclaim、sending timeout 之后的可恢复性
4. 补齐从 request finalize 到 outbox dispatch 的联调测试

### 6.4 完成标准

- `notification_outbox claim -> supervisor -> outbox_dispatch -> sent/retry/failed` 有 integration coverage。
- `noop` / `console` 等当前支持 channel 的成功路径可验证。
- dispatch 失败、重试或失败落库路径可验证。
- 如触达 `runtime_orchestrator` 或 `execution_supervisor`，提交说明中写清原因。

推荐停止点:

- 完成一条从 request finalize 产生 outbox，到 `outbox_dispatcher` claim，再到 handler 标记 `sent` 的端到端测试。
- 至少覆盖一个失败或 retry 落库场景。
- 如果发现要支持新的外部通知 channel，应记录为 follow-up，不在本轮扩展。

可合并信号:

- 可以清楚说明 outbox 的成功路径、失败路径和 retry/failed 状态写回在哪里被测试覆盖。
- `tests/test_execution_supervisor_runtime.py`、`tests/test_runtime_lifecycle.py`、`tests/test_runtime_store.py` 和新增 outbox integration 测试通过。

### 6.5 不要做的事

- 不要改 refresh / keyword 的共享 helper
- 不要改 watchdog 扫描规则本身
- 不要借这个窗口去重写 workflow runtime

### 6.6 建议验证

```bash
uv run pytest -q \
  tests/test_execution_supervisor_runtime.py \
  tests/test_runtime_lifecycle.py \
  tests/test_runtime_store.py \
  tests/test_runtime_phase2_ingest.py
```

如果补了新的 outbox 端到端测试，再把新文件加进去一起跑。

### 6.7 窗口首条提示词

```text
你现在在 /Users/happyzhao/Work/mujitask-wt-outbox-e2e 上工作。先阅读 docs/dev/worktree-parallel-development-handoff.md 的 Worktree B 部分，目标是把 outbox_dispatcher 和 outbox handler 路径做成可验证的端到端闭环。优先处理 notification_outbox claim、supervisor 包装、dispatch 成功/失败/重试、lease reclaim 和 integration tests，不要去改 refresh/keyword 共享 helper，也不要碰 watchdog rule。
```

## 7. Worktree C: watchdog-apply-integration

### 7.1 基本信息

- 分支: `codex/watchdog-apply-integration`
- 路径: `/Users/happyzhao/Work/mujitask-wt-watchdog-apply-integration`

### 7.2 负责范围

优先负责以下文件:

- `src/automation_business_scaffold/watchdog_scanner.py`
- `src/automation_business_scaffold/business/flows/watchdog_scanner.py`
- `src/automation_business_scaffold/infrastructure/runtime/runtime_store.py`
- 相关测试:
  - `tests/test_watchdog_scanner.py`
  - `tests/test_runtime_lifecycle.py`
  - 可新增 watchdog apply/integration tests

### 7.3 主要目标

这一条线的重点是把 Watchdog 从“决定动作”推进到“真正把动作写回 Runtime DB 并验证结果”。

建议优先处理:

1. 用真实 runtime 记录构造 lease expired / stale progress / execution timeout / waiting_children / outbox sending timeout 候选
2. 跑 `apply_actions=True` 的 scanner 路径
3. 验证 `retry` / `fail` / `repair` 对各目标表的持久化结果
4. 验证重复执行的幂等性和优先级去重

### 7.4 完成标准

- 使用真实 Runtime DB 记录覆盖至少一条 `retry` 路径。
- 使用真实 Runtime DB 记录覆盖至少一条 `fail` 或 `repair` 路径。
- `apply_actions=True` 的 scanner 路径可验证。
- 重复扫描不会造成不一致写回。

推荐停止点:

- 完成真实 Runtime DB apply integration，至少覆盖 `retry` 和 `fail` / `repair` 中的一类。
- 验证相同候选重复扫描不会重复破坏状态。
- 如果发现需要新增 Runtime DB 字段或 migration，应暂停回报，不在本 worktree 里直接扩 schema。

可合并信号:

- 可以清楚说明每条 Watchdog rule 的 apply 行为是否已覆盖；未覆盖的规则必须列为 follow-up。
- `tests/test_watchdog_scanner.py`、`tests/test_runtime_lifecycle.py`、`tests/test_runtime_store.py` 和新增 watchdog apply/integration 测试通过。

### 7.5 不要做的事

- 不要改 outbox handler 实现
- 不要改 refresh / keyword workflow helper
- 不要把 watchdog 逻辑再拆成新的架构层

### 7.6 建议验证

```bash
uv run pytest -q \
  tests/test_watchdog_scanner.py \
  tests/test_runtime_lifecycle.py \
  tests/test_runtime_store.py
```

如果新增了 apply/integration 测试，需要把新文件一起跑，并至少覆盖一次 `--once` 非 dry-run 场景。

### 7.7 窗口首条提示词

```text
你现在在 /Users/happyzhao/Work/mujitask-wt-watchdog-apply-integration 上工作。先阅读 docs/dev/worktree-parallel-development-handoff.md 的 Worktree C 部分，目标是把 watchdog 从 dry-run/decision 层推进到真实 apply integration。优先用真实 runtime 记录构造候选，覆盖 retry/fail/repair 写回和幂等性验证，不要去改 outbox handler，也不要动 refresh/keyword workflow helper。
```

## 8. 合并建议

建议合并顺序:

1. `workflow-common-helper`
2. `outbox-e2e`
3. `watchdog-apply-integration`

原因:

- `workflow-common-helper` 改的是共享 runtime workflow 代码，后续其他 workflow 线容易踩到这部分。
- `outbox-e2e` 和 `watchdog-apply-integration` 边界更独立，但都依赖当前 runtime lifecycle / supervisor 基座已经稳定。

每条线合并前，主窗口需要执行:

1. 确认该 worktree 分支有 commit，且工作区干净。
2. 阅读该 worktree 的回报格式。
3. 将该分支合并到 `codex/workflow-redesign-docs`。
4. 跑该线专项测试。
5. 三条线都合并后，再跑跨 runtime 回归。

如果某条线只完成了一部分，但已经满足推荐停止点，可以先合并；剩余内容作为下一轮 worktree 任务，不要要求当前窗口无限继续。

## 9. 主工作区职责

主工作区 `/Users/happyzhao/Work/mujitask` 继续作为集成区使用，负责:

- 收各条线的结果
- 跑跨 worktree 回归
- 做最终冲突处理
- 维护整体重构节奏

主工作区暂时不要再承担新的功能开发，以免和并行分支再次交叉写同一批文件。
