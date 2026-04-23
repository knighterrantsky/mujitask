# 重构验收契约

日期: 2026-04-23

## 1. 目的

本契约用于约束本轮业务层重构的开发边界、交付方式和验收标准。

本轮重构的目标不是在旧 `business` 代码之上做兼容包装，而是:

- 先把旧实现完整冻结到 `achieve/` 目录，作为黄金基准。
- 再按新的目标架构重新实现业务层:
  - `handler registry`
  - `workflow definition`
  - 通用 `job`
  - `executor_daemon / api_worker / browser_worker / outbox_dispatcher` 的新执行链路
- 最后以新旧行为对比作为验收依据。

## 2. 黄金基准

以下目录中的代码是本轮重构的黄金基准，只用于:

- 阅读和理解历史业务逻辑
- 设计新实现时核对输入 / 输出 / 状态流转
- 编写对比测试和验收脚本

黄金基准目录:

- `src/automation_business_scaffold/business/flows/achieve/`
- `src/automation_business_scaffold/business/tasks/achieve/`
- `src/automation_business_scaffold/business/workflows/achieve/`

## 3. 硬约束

### 3.1 禁止 Runtime 依赖 Achieve

新的运行时代码禁止以任何形式依赖 `achieve` 目录中的 Python 模块。

禁止形式包括但不限于:

- `from .achieve.xxx import ...`
- `from ..achieve.xxx import ...`
- `from automation_business_scaffold.business.*.achieve.xxx import ...`
- `importlib.import_module(...achieve...)`
- 通过 `sys.path`、动态路径拼接等方式间接加载 `achieve` 模块

`achieve` 只能作为阅读参考和验收基准，不能成为运行时依赖。

### 3.2 旧业务层必须先断开

在新实现开始落地前，`business/flows`、`business/tasks`、`business/workflows` 下 `achieve/` 之外的旧文件必须先删除。

这样做的目的只有一个:

- 保证新的代码不会在重构过程中意外调用旧实现

本轮重构期间，项目允许暂时不可运行，不以“保持旧入口兼容”作为约束。

### 3.3 新实现必须按目标架构落地

新代码必须围绕以下结构重建，而不是把旧业务函数重新拼回原目录:

- `business/handlers/`: 通用 handler 与 registry
- `business/workflow_defs/`: workflow / stage / job definition
- 新的 `business/flows/*.py`: 只保留编排、claim、reconcile、dispatch 逻辑
- 新的 `business/tasks/*.py`: framework task 入口
- 新的 `business/workflows/*.py`: workflow spec 入口

### 3.4 兼容包装不算完成

以下做法不视为完成重构:

- 只写一层 shim 再转调旧业务代码
- 保留旧 `flow/task/workflow` 逻辑不动，仅调整 import 路径
- 把旧业务专用 handler 原样搬回新架构并继续作为运行时主路径

## 4. 验收标准

本轮重构的验收以“行为一致性”而不是“文件名一致性”为准。

至少要满足:

1. 新实现与 `achieve` 基准在关键 workflow 上输出一致或在契约允许范围内一致。
2. 新实现通过新的架构契约测试。
3. 新实现通过新旧对比测试。
4. 业务层运行时代码中不存在任何 `achieve` import。

关键 workflow 包括:

- `refresh_current_competitor_table`
- `search_keyword_competitor_products`
- `sync_tk_influencer_pool`
- `tiktok_fastmoss_product_ingest`

## 5. 对比测试要求

重构测试至少包含两类:

- 架构约束测试:
  - 禁止 runtime import `achieve`
  - handler registry 只允许准入清单中的 handler
  - workflow definition / stage / job 命名符合 contract
- 行为对比测试:
  - 以 `achieve` 为基准，对关键 workflow 的输入 / 中间状态 / 最终输出进行对照

## 6. 交付顺序

推荐交付顺序如下:

1. 删除 `business/flows`、`business/tasks`、`business/workflows` 中 `achieve/` 外的旧文件。
2. 建立 `handler registry` 与 `workflow definition` 的新骨架。
3. 优先重建四个正式 workflow 的编排链路。
4. 为关键 workflow 增加新旧行为对比测试。
5. 清理遗留兼容名称和历史业务专用 job / handler。

## 7. 当前检查点

当前检查点的要求是:

- 旧业务代码已经集中冻结到 `achieve/`
- `business/flows`、`business/tasks`、`business/workflows` 外层旧文件允许先清空
- 后续新增文件必须默认视为“新架构代码”，不得回连 `achieve`

