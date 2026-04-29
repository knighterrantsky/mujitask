# 模块实现所有权契约

日期: 2026-04-24

状态: 受控架构契约

## 1. 定位

本文定义每类模块必须“拥有”哪些实现，以及哪些模块只能登记、声明或组合，不能替别人承载实现。它补充 [项目架构契约](./project-architecture-contract.md)、[Workflow 实现模式规范](./workflow-implementation-patterns.md)、[真实迁移 Checklist](./real-migration-checklist.md)、[项目结构与命名契约](./project-structure-contract.md) 和 [飞书表 Adapter 与 Projection Mapper 契约](./feishu-table-adapter-projection-contract.md)。

本文解决五类反复混淆的问题:

- domain mapper / projection 的业务字段所有权。
- capability handler 的外部能力实现所有权。
- `__init__.py`、registry、common 模块的有限职责。
- legacy business 路径在迁移期和完成后的边界。
- 禁止 thin wrapper、显式 re-export、handler-to-handler 实现复用、一文件多 handler、`_implementations` 大杂烩。

一句话规则:

> 业务字段差异由 domain mapper/projection 拥有，外部能力由 capability handler 拥有，registry 只登记，common 只放无业务语义的小型工具，`__init__.py` 只标记包；任何运行时主路径都不得靠转发、包装或聚合文件假装完成迁移。

## 2. 实现所有权定义

“拥有实现”指模块内直接定义本模块对外承诺的主要行为，而不是把行为转发到另一个实现模块。

一个拥有实现的文件通常应包含:

- 稳定 code 常量，例如 `HANDLER_CODE`、`MAPPER_CODE`、`PROJECTION_CODE`。
- 本模块对外导出的主函数或主类。
- 与该主函数或主类直接相关的 parsing、validation、normalization、helper。
- 本模块边界内的错误分类、默认值和结果结构。

不算拥有实现:

- 只有 `from ... import ...`。
- 只有函数调用转发，例如 `return other_handler(payload)`。
- 只有 alias，例如 `xxx = old_xxx`。
- 只有 registry 绑定或 `__all__`。
- 通过 `sys.modules`、module alias 或 import hook 替换模块身份。
- 把多个 handler / mapper / projection 的主体塞进 `_implementations.py`、`common.py` 或 `registry.py`。

## 3. Domain Mapper / Projection 所有权

目标路径:

```text
src/automation_business_scaffold/domains/{domain}/mappers/{source}_{business_object}_mapper.py
src/automation_business_scaffold/domains/{domain}/projections/{target}_{view}_projection.py
```

mapper 拥有:

- 输入源行、事实源结果或中间 result 到 domain object 的字段解释。
- 业务字段名、兼容别名、默认值和标准化规则。
- domain key、candidate、seed、writeback context 等业务对象构造。
- 纯函数式字段转换和业务校验。
- 飞书 source adapter 的候选字段集合、字段判断逻辑、跳过原因和 adapter summary。

projection 拥有:

- domain object / workflow result 到飞书表格、消息、视图或 outbox payload 的字段投影。
- 写回字段名、列级默认值、展示格式和缺失值策略。
- 面向外部通道的业务摘要文本，但不负责发送。
- 飞书 projection mapper 的必填字段、可选字段、人工保留字段和系统覆盖字段策略。

mapper / projection 禁止:

- 调用 Feishu / TikTok / FastMoss / AWS / MinIO / DB client。
- claim job、推进 workflow、写 Runtime DB / Fact DB / Object Store。
- 作为 handler registry key。
- 通过 `__init__.py` 或 registry 显式 re-export 给旧路径使用。
- 多个业务对象共用一个大 mapper 文件承载主体逻辑。

允许复用的方式:

- 同一 domain 内可抽小型纯函数到 `{business_object}_fields.py` 或 `{business_object}_normalization.py`。
- 跨 domain 复用必须抽到无业务字段语义的 contracts/model helper；不能把某个业务域 mapper 作为另一个业务域的主实现。
- mapper 可以被 job payload rendering 或 workflow summary policy 引用，但引用方不能接管 mapper 的字段逻辑。

## 4. Capability Handler 所有权

目标路径:

```text
src/automation_business_scaffold/capabilities/{category}/{system}/{capability}_handler.py
```

capability handler 拥有:

- `HANDLER_CODE`、`CONTRACT` 和 handler 主函数。
- payload parsing 和 handler-level validation。
- 外部 transport、client / store 调用、分页、批量、限速、重试分类。
- 标准 `HandlerResult` / result envelope 构造。
- capability 范围内的幂等、错误分类和 artifact 引用。

capability handler 禁止:

- 写 domain 专属字段筛选、业务投影、终态判断或 summary 规则。
- 直接导入 domain mapper / projection / policy 作为实现依赖。
- 调用另一个 handler 的主函数来完成自己的主体实现。
- 在一个文件里定义多个 handler 的主体实现。
- 从旧 `business/handlers/**`、`.implementations` 或 `_implementations` 转发主实现。

handler-to-handler 实现复用禁止规则:

- handler A 不能通过调用 handler B 的 `handle_*` 主函数来完成自己的主要工作。
- 如果两个 handler 需要共用底层能力，应抽到 capability 内部 client helper、batch helper、transport helper 或 infrastructure client。
- 复用 helper 必须表达技术能力，不表达另一个 handler 的 contract、payload 或 result envelope。

示例:

```text
允许: table_write_handler.py -> feishu_batch_writer.py -> infrastructure client
禁止: table_append_handler.py -> table_write_handler.handle_table_write(...)
```

## 5. `__init__.py` 契约

`__init__.py` 只用于声明 Python package 或极轻量 package metadata。

允许:

- 文件为空。
- 包级 docstring。
- 无副作用常量，例如 `__version__`，且不承载 runtime contract。

禁止:

- 显式 re-export mapper / projection / handler / job / workflow 主函数。
- 维护 `__all__` 作为业务导出面。
- import 旧路径以制造兼容入口。
- 注册 handler、注册 mapper、连接外部 client 或读取配置。
- 用 `__getattr__`、lazy import、module alias 隐藏真实实现位置。

调用方必须导入真实模块路径，例如:

```python
from automation_business_scaffold.domains.tiktok.mappers.feishu_competitor_row_mapper import map_competitor_row
```

禁止依赖包级入口，例如:

```python
from automation_business_scaffold.domains.tiktok.mappers import map_competitor_row
```

## 6. Legacy Business 路径边界

Legacy `business/` 目录已完成迁移并删除。迁移任务开始后，必须声明是 `scaffold` 还是 `real_migration`。

`scaffold` 允许:

- 建项目目录、空模块、manifest、TODO 和迁移计划。
- 在文档中记录旧路径和目标路径对应关系。
- 不宣称实现所有权已迁移完成。

`real_migration` 要求:

- 目标 domain / capability 文件拥有真实实现。
- runtime registry、workflow manifest、测试导入目标路径。
- legacy 路径不再作为 runtime 主路径。
- 旧实现只作为阅读参考、行为对照或 fixture 来源。

legacy 路径禁止:

- 新增 thin wrapper 包装目标路径后继续作为 runtime 主路径。
- re-export 目标 domain / capability 以维持旧 import。
- 继续持有主实现，同时让目标路径只做导入转发。
- 通过 monkeypatch 需求、旧测试或旧脚本兼容性阻止 runtime import 主路径迁移。

## 7. Registry 模块契约

registry 是“登记表”，不是“实现仓库”。

允许:

- 保存稳定 code 到 callable / contract 的映射。
- 做准入校验、重复 code 检查和错误提示。
- 延迟导入真实模块以降低启动成本，但导入目标必须是拥有实现的文件。
- 暴露 `get_*`、`list_*`、`register_*` 这类登记 API。

禁止:

- 定义 handler / mapper / projection 主体逻辑。
- 把业务字段转换写成 registry 分支。
- 用 registry 根据业务类型调用多个 handler 来拼出一个 handler 的实现。
- 在 registry 中修正 payload、result 或业务字段兼容。
- 把未准入的 adapter / mapper / projection 注册成 runtime handler code。

registry 依赖方向:

```text
handler registry -> capability handler module
mapper registry -> domain mapper module
projection registry -> domain projection module
```

反向禁止:

```text
capability handler -> domain mapper registry
domain mapper/projection -> handler registry
```

## 8. Common 模块契约

common 模块只承载小型、稳定、无业务归属的工具。

允许放入 common:

- 类型无关的字符串、日期、数字、分页和 batch helper。
- 无业务字段名的 schema / payload 小工具。
- 无外部副作用的纯函数。
- 多个同类模块都需要、且无法归属到单一 handler / mapper / projection 的技术 helper。

禁止放入 common:

- handler 主函数或 handler-specific payload/result envelope。
- 飞书表字段、TikTok/FastMoss 业务字段、客户验收字段。
- workflow summary、终态、outbox 文案或投影规则。
- 为避免文件变大而搬出的私有业务逻辑。
- 跨 handler 的“万能执行器”。

命名要求:

- common 文件名必须表达技术用途，例如 `batching.py`、`date_parsing.py`、`payload_validation.py`。
- 禁止使用 `common_business.py`、`common_handlers.py`、`shared_logic.py`、`helpers.py` 这类无法表达所有权的名称作为新增主路径。

## 9. 明确禁止模式

以下模式在新增代码和 `real_migration` 中一律禁止:

| 禁止模式 | 定义 | 正确做法 |
| --- | --- | --- |
| thin wrapper | 新文件只做参数透传、调用旧函数或目标函数 | 把主体实现移动到拥有所有权的文件 |
| 显式 re-export | `from old_or_other_module import x` 后作为本模块导出 | 调用方改为导入真实模块路径 |
| handler-to-handler 实现复用 | 一个 handler 调另一个 handler 主函数完成主体行为 | 抽技术 helper 或 infrastructure client |
| 一文件多 handler | 一个 `{capability}_handler.py` 定义多个 handler 主函数和 contract | 每个 handler 一个文件，公共技术逻辑抽 helper |
| `_implementations` 大杂烩 | 用聚合文件承载多个模块主体实现 | 按 domain / capability / mapper / projection 拆回真实文件 |
| `__init__.py` 导出面 | 包级导出替代真实模块导入 | 显式导入具体文件 |
| registry 分支实现 | registry 根据 code 写业务分支和转换逻辑 | registry 只查找拥有实现的 callable |
| legacy 主路径兼容 | 旧路径转发目标实现后仍被 runtime 使用 | runtime registry 和测试改到目标路径 |

## 10. 评审 Checklist

修改 mapper / projection / capability / registry / common / legacy 路径时，评审必须逐项确认:

- [ ] 新增或修改的主函数在拥有所有权的文件内真实定义。
- [ ] 没有新增 thin wrapper、显式 re-export 或 `sys.modules` alias。
- [ ] 没有新增 `_implementations`、`shared_logic.py`、万能 `helpers.py` 大杂烩。
- [ ] 每个 capability handler 文件只拥有一个 handler contract。
- [ ] handler 没有调用另一个 handler 主函数实现主体行为。
- [ ] `__init__.py` 没有导出 runtime contract。
- [ ] registry 只登记，不承载业务转换和 handler 实现。
- [ ] common helper 不包含业务字段、projection、summary 或 handler envelope。
- [ ] 飞书表 source adapter/projection mapper 的字段集合和缺失策略符合 [飞书表 Adapter 与 Projection Mapper 契约](./feishu-table-adapter-projection-contract.md)。
- [ ] legacy business 路径没有继续作为 runtime 主路径。
- [ ] 测试导入真实目标模块路径，并覆盖 mapper/projection/capability 的行为。

## 11. 测试护栏

模块实现所有权应由静态检查和行为测试共同守住。

建议测试:

| 测试 | 守护内容 |
| --- | --- |
| module ownership static test | 禁止 thin wrapper、显式 re-export、`sys.modules` alias、`_implementations`、`__init__.py` 导出面 |
| handler ownership test | 每个 handler 文件只有一个 handler code，且主函数在本文件定义 |
| registry ownership test | registry 只登记拥有实现的目标模块，不承载业务分支 |
| mapper/projection behavior test | fixture 输入到 domain object / writeback projection 的行为归属 |
| legacy import path test | runtime registry、manifest 和测试不再使用 legacy business 主路径 |

如果结构测试需要例外，例外必须写明到期条件、迁移任务和目标模块；不能用永久 ignore 掩盖所有权不清。
