# 飞书表 Adapter 与 Projection Mapper 契约

日期: 2026-04-24

状态: 受控架构契约

## 1. 定位

本文定义飞书表读取 `source adapter` 和飞书表写入 `projection mapper` 的配置定义、功能定义和所有权边界。它补充 [项目结构与命名契约](./project-structure-contract.md)、[模块实现所有权契约](./module-ownership-contract.md) 和 [Handler Contract 设计](./handler-contract-design.md)。

本文不新增新的 runtime 机制，不要求引入新的配置系统，也不改变当前 `feishu_table_read` / `feishu_table_write` 架构。现有 `adapter_code` / `mapper_code` 只是选择已有模块的路由标识；真正需要被约束的是被选中的 source adapter / projection mapper 模块本身。

一句话规则:

> Source adapter 模块必须同时定义“读表字段配置”和“读表处理逻辑”；projection mapper 模块必须同时定义“写表字段配置”和“写表处理逻辑”；具体飞书表字段不得出现在 common、handler、registry 或其他无关文件里。

采集原始数据到 `fact_bundle`、`fact_bundle` 到 Fact DB 的标准化不在本文范围内。

## 2. 模块配置定义

这里的“配置定义”不是指必须新增外部配置文件，也不是指把字段都放到 workflow 里。配置定义指 source adapter / projection mapper 模块必须清楚表达本模块处理哪张表、哪些字段、哪些规则。

配置定义可以是模块内常量、字典、dataclass、小型纯函数，或从 payload/table profile 读取后在模块内归一化出的结构。无论形式如何，必须满足:

- 配置和处理逻辑在同一个拥有所有权的模块内维护。
- 字段列表必须完整覆盖该模块实际读取或写入的字段。
- 每个字段集合都必须有明确用途，不能只有字段名没有规则。
- 多张表字段不同但复用同一个模块时，必须显式传入并归一化字段配置，不能使用隐藏的历史默认字段。
- common、handler、registry 不能定义或补全业务字段配置。

## 3. Source Adapter 配置定义

Source adapter 的配置定义描述“当前飞书来源表应该怎么读、怎么判断、怎么筛选”。

至少应定义以下内容:

| 配置项 | 说明 |
| --- | --- |
| 表/业务对象定位 | 说明该 adapter 服务哪类飞书来源表或业务对象 |
| 身份字段 | 用于识别业务对象的字段，例如商品 ID、商品链接、达人 ID 等 |
| 字段别名 | 同一语义字段在不同表中的可能列名 |
| 候选判断字段 | 哪些字段参与候选判断，例如 15 个字段里只检查其中 12 个 |
| 状态字段 | 哪些字段决定跳过、可处理、不可售、已完成等状态 |
| 透传字段 | 不参与判断但需要放入 `source_context` 或后续写回上下文的字段 |
| 缺失策略 | 身份字段、候选字段、状态字段缺失时是跳过、报配置错误还是继续处理 |
| 去重策略 | `business_key` 或候选 key 如何生成 |
| summary 口径 | `skipped_*_count`、`dropped_*_count`、`deduped_count` 等统计含义 |

如果某个业务说“当前表 15 个字段，只判断 12 个字段”，这 12 个字段必须出现在 source adapter 的配置定义中，并且要说明:

- 哪些字段是身份字段。
- 哪些字段只是判断候选是否需要处理。
- 哪些字段缺失会导致行被丢弃。
- 哪些字段缺失只是作为 `missing_fields` 输出给后续流程。

禁止把这种字段集合放到 `table_common.py` 或其他通用文件里。

## 4. Source Adapter 功能定义

Source adapter 的功能是把飞书原始行转换成 workflow 可消费的业务候选对象。

输入是 `feishu_table_read` 已经标准化后的 `raw_rows`，每行包含 `record_id`、`fields`、`created_time`、`updated_time` 等通用信息。

输出必须包含:

| 输出项 | 说明 |
| --- | --- |
| `source_rows` | 被选中的业务候选行 |
| `candidate_keys` | 候选业务 key，用于 fan-out、去重或 summary |
| `adapter_summary` | adapter 执行摘要，包含输入数量、输出数量、跳过原因等 |

`source_rows` 中应尽量包含:

- `source_record_id`
- `source_table_ref`
- `business_key`
- 当前业务对象 identity，例如 `product_identity`
- `business_fields`
- `writeback_context`
- `source_context.source_fields`

Source adapter 只能做纯业务语义转换和行级判断。它不能:

- 调用 Feishu / TikTok / FastMoss / DB / Object Store client。
- 写飞书表、Runtime DB、Fact DB 或 artifact。
- 推进 workflow stage、claim job 或构造 HandlerResult。
- 在运行时临时补一套不完整字段配置。

## 5. Projection Mapper 配置定义

Projection mapper 的配置定义描述“当前飞书目标表应该写哪些字段、哪些字段必须完整、哪些字段可以缺省、哪些字段可以覆盖”。

至少应定义以下内容:

| 配置项 | 说明 |
| --- | --- |
| 表/业务对象定位 | 说明该 mapper 写回哪类飞书目标表或业务对象 |
| 写入身份 | `record_id`、`upsert_key`、`business_entity_key` 如何确定 |
| 必填写入字段 | 缺失任意一个就不能认为本次采集可成功写回的字段 |
| 可选写入字段 | 有数据就写，没有数据不阻断写回的字段 |
| 系统覆盖字段 | 系统允许覆盖已有值的字段，例如状态、记录日期 |
| 人工保留字段 | 默认不覆盖已有人工维护值的字段 |
| 字段取值来源 | 每个写入字段从 record、payload、fact/result、source_context 的哪里取值 |
| 格式化规则 | 链接、图片、日期、数字、列表等飞书字段格式 |
| 缺失策略 | 必填字段缺失时失败、跳过、部分写入还是只写状态字段 |

如果某张目标表有 15 个字段，其中 8 个是必填、4 个是可选，projection mapper 的配置定义必须把这两类字段写清楚。不能只在处理函数里散落字段名，也不能只靠 `validate_write_schema` 兜底。

`validate_write_schema` 只确认飞书表是否存在这些列；它不负责判断采集结果是否完整，也不负责决定哪些字段可以不写。

## 6. Projection Mapper 功能定义

Projection mapper 的功能是把业务结果、workflow result 或 fact/result payload 转换成 `feishu_table_write` 可执行的写入命令。

输出写入命令应明确:

| 输出项 | 说明 |
| --- | --- |
| `op` | append、update、upsert、insert_if_absent、delete 等写入动作 |
| `record_id` | 更新或删除已有行时的飞书 record id |
| `business_entity_key` | 业务幂等 key |
| `upsert_key` | 无 record id 时用于查找已有行的唯一字段 |
| `fields` | 本次实际写入飞书的字段 |
| `source_context` | 写回来源上下文 |

Projection mapper 必须根据配置定义处理字段:

- 必填字段缺失时，不能静默写入不完整字段。
- 可选字段为空时应省略。
- 人工保留字段默认不覆盖已有值。
- 系统覆盖字段必须显式列出后才允许覆盖。
- 写入 key 必须稳定，避免重复创建或误更新。

Projection mapper 不能:

- 调用 Feishu / TikTok / FastMoss / DB / Object Store client。
- 执行 batch、retry、pagination、rate limit 或 Feishu API 错误分类。
- 把字段缺失策略放到 handler、registry 或 common。
- 通过不相关模块补全字段配置。

## 7. 所有权边界

飞书表字段配置的所有权如下:

| 位置 | 可以做什么 | 不能做什么 |
| --- | --- | --- |
| source adapter 模块 | 定义读表字段配置和候选判断逻辑 | 调外部 client、写库、写飞书 |
| projection mapper 模块 | 定义写表字段配置和写入投影逻辑 | 调外部 client、写库、执行 batch |
| Feishu handler | 读写飞书、分页、schema 校验、错误分类、结果 envelope | 定义业务字段、判断候选、决定必填/可选 |
| registry | 根据已有标识找到模块 | 写字段分支、补业务规则 |
| common/helper | 无业务字段语义的小工具 | 放具体飞书表字段列表 |
| workflow/flow | 传入本次运行上下文或 table profile | 散落字段名替代 adapter/mapper 配置定义 |

这条规则用来防止再次出现“在一个不相关文件中出现配置字段，而且字段还不完整”的问题。

## 8. 完整性要求

新增或修改 source adapter / projection mapper 时，必须检查配置定义是否完整:

- 模块实际读取或写入的每个字段，都能在配置定义中找到归属。
- 配置定义中的每个字段，都能在处理逻辑中找到使用方式。
- 字段缺失时的行为明确。
- 支持多张表时，差异字段通过显式配置进入模块，并在模块内归一化。
- 测试覆盖完整字段、缺失字段、可选字段、人工保留字段、系统覆盖字段等关键路径。

当前代码中如存在以下情况，应作为后续重构项修复:

- `table_common.py` 中残留具体业务字段、adapter 或 projection 逻辑。
- source adapter 使用隐藏默认字段列表代表多张表。
- projection mapper 未显式声明必填/可选/人工保留/系统覆盖字段策略。
- workflow 中散落字段名，但对应 adapter/mapper 模块没有完整配置定义。
