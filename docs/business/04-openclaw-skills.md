# OpenClaw Skills

更新时间：`2026-04-07`

本文件面向 OpenClaw skill 配置与运行，描述 v3.0 的 skill 设计方案，并明确当前 skill 基线与目标形态之间的差距。

## 1. Skill 定位

v3.0 的 skill 采用单 skill、多入口设计。

skill 名称继续沿用：

- `mujitask-tiktok-feishu-sync`

但业务定位升级为：

- TK 竞品采集与更新 skill

对 OpenClaw 暴露两类业务语义：

1. 定时更新当前飞书竞品表。
2. 按关键词搜索 FastMoss 竞品并写入飞书。

设计原则：

- OpenClaw 不需要知道底层 task 名、workflow 名或脚本拆分。
- 一个 skill 同时覆盖“表单更新”和“关键词找品”两类入口。
- skill 对外暴露业务语义，对内复用已有内部流程。

## 2. Skill 包结构设计

### 2.1 当前包结构基线

当前 skill 包中已存在的主要文件包括：

- `SKILL.md`
- `skill.local.env`
- `skill.local.env.example`
- `run_feishu_tiktok_sync.sh`
- `run_feishu_tiktok_sync.ps1`
- `run_cleanup.sh`
- `run_cleanup.ps1`
- `run_batch_sync.sh`
- `run_batch_sync.ps1`
- `resolve_browser_target.py`
- `openclaw_result.py`
- `start_browser_cdp.sh`
- `start_browser_cdp.ps1`

### 2.2 v3.0 目标结构

v3.0 的 skill 结构逻辑上分为三层：

1. 主入口分发层
   - 对外只暴露一个主入口脚本
   - 接收 OpenClaw 触发
   - 根据入口类型分发到不同内部包装流

2. 内部包装层
   - 定时更新包装流
   - 关键词搜索包装流
   - 负责把 OpenClaw 业务语义映射为内部执行顺序

3. 结果汇总层
   - 汇总各阶段结果
   - 输出统一的 `__OPENCLAW_RESULT__ <json>`

说明：

- 当前已存在的 `run_cleanup.*`、`run_batch_sync.*`、`run_feishu_tiktok_sync.*` 属于现有基线脚本。
- v3.0 目标设计要求新增关键词入口包装能力，但本文件只描述设计，不代表当前脚本已经全部实现。

## 3. Skill 本地配置设计

skill 本地持久化配置文件固定为：

- `skill.local.env`

v3.0 的本地配置项应包含：

- `INSTALL_DIR`
- `TABLE_URL`
- `FEISHU_ACCESS_TOKEN`
- `BROWSER_PROFILE_REF`
- `FASTMOSS_PHONE`
- `FASTMOSS_PASSWORD`

配置边界：

- `TABLE_URL` 固定由 skill 配置提供，不由用户对话输入。
- `FEISHU_ACCESS_TOKEN` 固定由 skill 配置提供。
- `FASTMOSS_PHONE` 和 `FASTMOSS_PASSWORD` 固定由 skill 配置提供。
- `关键词` 和 `7日销量阈值` 只来自用户对话或调度参数，不进入 skill 固定配置。

浏览器配置约束：

- `BROWSER_PROFILE_REF` 优先从 `skill.local.env` 读取。
- 若未配置，则继续回退到项目 `.env` 中的 `DEFAULT_PROFILE_REF`。
- skill 不直接绑定 `chrome_cdp` 或 `roxy`，而是由浏览器 profile 配置决定。

## 4. OpenClaw 调用语义与分发

### 4.1 定时更新入口

适用场景：

- OpenClaw 定时任务
- 用户明确要求“更新当前表”

推荐业务语义：

- 定时更新当前飞书竞品表
- 更新当前飞书表中的 TK 竞品数据
- 对当前竞品表执行每日同步

固定执行顺序：

1. 链接标准化去重
2. 识别待更新行
3. 对待更新行逐条执行商品详情更新流

### 4.2 关键词搜索入口

适用场景：

- 用户在 OpenClaw 对话中输入关键词找品需求

推荐业务语义：

- 帮我查询关键字为 `east egg` 的 7 日内销量大于 200 的 TK 商品数据
- 搜索 FastMoss 中关键词为 `easter egg` 的商品，并把 7 天销量大于 300 的结果写入飞书
- 收集关键词为 `graduation gifts` 的 TK 竞品，并写入当前飞书表

从用户输入中需要提取：

- `关键词`
- `7日销量阈值`

固定执行顺序：

1. 通过 FastMoss 搜索候选商品
2. 固定按 `近7天销量` 排序
3. 固定翻页穷举结果
4. 两级判重：先 `SKU-ID`，再标准化后的 `产品链接`
5. 已存在商品直接跳过
6. 新商品先写入 `SKU-ID`
7. 对新商品执行商品详情更新流
8. 对新商品在 `备注` 中写入 `通过搜索关键字：{关键词}`

### 4.3 分发规则

主入口分发遵循以下规则：

1. 如果触发来源是 OpenClaw 定时任务，则固定进入定时更新入口。
2. 如果用户输入中包含明确的关键词搜索意图和销量阈值，则进入关键词搜索入口。
3. 如果用户表达的是“更新当前飞书表”“同步当前表单”“执行每日抓取”等语义，则进入定时更新入口。

## 5. Skill 运行时与输出协议

### 5.1 运行中输出

v3.0 skill 继续复用当前同步输出协议，不另起新协议。

运行中继续输出：

- 阶段日志
- `run_id`
- `run_file`
- `steps_file`
- 心跳日志

### 5.2 最终结果行

主入口结束前仍然输出：

```text
__OPENCLAW_RESULT__ <json>
```

### 5.3 v3.0 结果 JSON 设计

在不改变固定尾行协议的前提下，v3.0 skill 结果 JSON 应统一包含：

- `status`
- `task_name`
- `entry_type`
- `message`
- `summary`
- `detail`
- `error`

其中：

- `entry_type = scheduled_update`
- `entry_type = keyword_search`

对两类入口的摘要要求：

1. 定时更新入口
   - `cleanup_summary`
   - `target_row_count`
   - `updated_count`
   - `skipped_count`
   - `failed_count`

2. 关键词搜索入口
   - `search_keyword`
   - `sales_7d_threshold`
   - `matched_count`
   - `skipped_existing_count`
   - `inserted_count`
   - `completed_count`
   - `failed_count`

说明：

- 当前 [05-openclaw-output-protocol.md](./05-openclaw-output-protocol.md) 仍以 cleanup/batch 结果为基线。
- v3.0 这里描述的是 skill 设计目标，用于后续扩展协议字段，而不是声明当前 helper 已经支持这些字段。

## 6. Skill 执行规则

### 6.1 定时更新模式

skill 执行定时更新模式时，规则固定为：

1. 只读取 `skill.local.env` 中的本地业务配置。
2. 先处理 `产品链接` 的标准化与去重。
3. 仅基于自动维护字段识别待更新行。
4. 对每条待更新记录执行商品详情更新流。
5. 商品详情更新流中必须先抓 TikTok 信息，再进入 FastMoss 详情页。
6. 打开 FastMoss 详情页后先截图，再抓取销量信息。

### 6.2 关键词搜索模式

skill 执行关键词搜索模式时，规则固定为：

1. 只允许从用户输入中提取 `关键词` 与 `7日销量阈值`。
2. 数据源固定为 FastMoss。
3. 搜索结果固定按 `近7天销量` 排序。
4. 搜索结果固定翻页穷举。
5. 候选商品必须执行两级判重：
   - 先按 `SKU-ID`
   - 再按标准化后的 `产品链接`
6. 只要命中任一判重条件，都视为已存在商品并直接跳过。
7. 只对新商品执行写入与补全。

## 7. 当前基线与目标差距

当前基线：

- 当前 skill 只有一个“cleanup + batch sync”主入口。
- 当前 skill 主要覆盖 TikTok 批量更新语义。
- 当前 skill 本地配置中还未包含 FastMoss 登录信息。
- 当前输出 helper 仍以 cleanup / batch 结构为主。

v3.0 目标：

- 一个 skill 覆盖“定时更新”和“关键词搜索”两类入口。
- skill 本地配置纳入 `FASTMOSS_PHONE / FASTMOSS_PASSWORD`。
- FastMoss 关键词候选发现流进入主 skill 设计。
- FastMoss 商品详情截图与销量写回进入主更新流。
- 结果摘要能够通过 `entry_type` 区分入口类型。

说明：

- 本文件描述的是 v3.0 skill 设计目标。
- 当前代码基线并不代表这些能力已经全部落地。

## 8. 常见错误

### 8.1 缺少 `skill.local.env`

- 无法读取本地业务配置
- 处理方式：从 `skill.local.env.example` 复制并填写

### 8.2 缺少 `TABLE_URL`

- 无法定位目标飞书表
- 处理方式：补充正确的飞书表地址

### 8.3 缺少 `FEISHU_ACCESS_TOKEN`

- 无法读取或写回飞书数据
- 处理方式：补充有效的飞书 token

### 8.4 缺少 `FASTMOSS_PHONE` 或 `FASTMOSS_PASSWORD`

- 无法完成关键词入口或 FastMoss 详情更新流
- 处理方式：在 `skill.local.env` 中补充 FastMoss 登录信息

### 8.5 浏览器 profile 未就绪

- 无法进入 TikTok 或 FastMoss 页面抓取
- 处理方式：`chrome_cdp` 模式下先启动对应 CDP 端点；`roxy` 模式下检查 `ROXY_HOST`、`ROXY_TOKEN` 与 profile 配置

## 9. 可运行判定

只有同时满足下面条件，才能认为 v3.0 skill 已具备运行基础：

- OpenClaw workspace 中存在 `mujitask-tiktok-feishu-sync`
- skill 目录中存在 `SKILL.md`、主入口脚本和结果 helper
- `skill.local.env` 已生成
- 本地项目 `.venv` 已存在
- `TABLE_URL`、`FEISHU_ACCESS_TOKEN` 已配置
- 浏览器 profile 已配置
- 如果需要 FastMoss 能力，则 `FASTMOSS_PHONE` 和 `FASTMOSS_PASSWORD` 已配置

## 10. 关联文档

- [01-需求文档.md](./01-需求文档.md)
- [02-设计文档.md](./02-设计文档.md)
- [03-部署文档.md](./03-部署文档.md)
- [05-openclaw-output-protocol.md](./05-openclaw-output-protocol.md)
- [../../skills/mujitask-tiktok-feishu-sync/SKILL.md](../../skills/mujitask-tiktok-feishu-sync/SKILL.md)
