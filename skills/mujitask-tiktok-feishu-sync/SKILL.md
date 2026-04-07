# mujitask-tiktok-feishu-sync

TK 竞品采集与更新 skill。

本文件描述 v3.0 的 skill 目标设计，并说明当前实现基线。  
它的目的不是暴露底层 task，而是让 OpenClaw 用一个 skill 同时触发“定时更新”和“关键词搜索”两类业务。

## Skill 定位

这个 skill 的目标定位是：

- 使用当前飞书竞品表做定时更新
- 通过 FastMoss 关键词搜索发现新竞品
- 统一回写飞书表中的自动维护字段

对 OpenClaw 暴露两类业务入口：

1. 定时更新当前飞书竞品表
2. 按关键词搜索 FastMoss 竞品并写入飞书

## 适用场景

- 飞书多维表格中已经维护了部分竞品链接
- 需要每天定时更新表中竞品数据
- 需要通过关键词搜索批量发现新商品
- 需要把 TikTok 信息、FastMoss 截图和销量数据统一写回飞书

## 本地配置

skill 默认从当前目录下的 `skill.local.env` 读取本地配置。

v3.0 目标配置包括：

- `INSTALL_DIR`
- `TABLE_URL`
- `FEISHU_ACCESS_TOKEN`
- `BROWSER_PROFILE_REF`
- `FASTMOSS_PHONE`
- `FASTMOSS_PASSWORD`

配置边界：

- `TABLE_URL` 不从用户对话输入
- `FEISHU_ACCESS_TOKEN` 不从用户对话输入
- `FASTMOSS_PHONE` 和 `FASTMOSS_PASSWORD` 不从用户对话输入
- `关键词` 和 `7日销量阈值` 只来自用户输入或调度参数

## 对外业务语义

### 1. 定时更新入口

适合下面这类触发：

- 更新当前飞书竞品表
- 执行每日竞品数据同步
- 对当前飞书表做定时刷新

这个入口的目标执行顺序是：

1. 标准化并去重 `产品链接`
2. 识别待更新行
3. 对待更新行逐条执行商品详情更新

### 2. 关键词搜索入口

适合下面这类触发：

- 帮我查询关键字为 `east egg` 的 7 日内销量大于 200 的 TK 商品数据
- 搜索 FastMoss 中关键词为 `easter egg` 的商品并写入飞书
- 收集关键词为 `graduation gifts` 的 TK 竞品

这个入口的目标执行顺序是：

1. 从用户输入中提取 `关键词`
2. 从用户输入中提取 `7日销量阈值`
3. 通过 FastMoss 搜索候选商品
4. 固定按 `近7天销量` 排序并翻页穷举
5. 先按 `SKU-ID` 判重，再按标准化后的 `产品链接` 判重
6. 已存在商品直接跳过
7. 新商品先写入 `SKU-ID`
8. 再补全其他自动维护字段
9. 对新商品在 `备注` 中写 `通过搜索关键字：{关键词}`

## 入口分发规则

主入口分发遵循以下规则：

1. OpenClaw 定时任务固定进入定时更新入口。
2. 用户输入中如果同时包含关键词搜索意图和销量阈值，则进入关键词搜索入口。
3. 用户输入如果表达的是“更新当前表”“同步当前竞品表”，则进入定时更新入口。

## 固定执行规则

- OpenClaw 负责顶层编排，skill 对外优先暴露确定性 step 脚本。
- `TABLE_URL` 固定来自 `skill.local.env`。
- 所有业务数据最终都写回同一张飞书表。
- 定时更新入口必须先执行链接标准化与去重。
- 商品详情更新时，必须先抓 TikTok 信息，再进入 FastMoss 详情页。
- 打开 FastMoss 商品详情页后必须先截图，再抓取销量信息。
- 关键词搜索入口必须固定使用 FastMoss 作为数据源。
- 关键词搜索结果必须固定按 `近7天销量` 排序并翻页穷举。
- 关键词候选商品必须执行两级判重：
  - 先按 `SKU-ID`
  - 再按标准化后的 `产品链接`

## 输出协议

主入口继续复用当前同步输出协议：

- 运行中输出阶段日志、`run_id`、进度文件路径和心跳日志
- 结束前输出固定尾行：`__OPENCLAW_RESULT__ <json>`

v3.0 的目标结果摘要应包含：

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

说明：

- 当前 [docs/business/05-openclaw-output-protocol.md](/Users/happyzhao/Work/mujitask/docs/business/05-openclaw-output-protocol.md#L1) 仍以 cleanup / batch 结果为基线
- v3.0 这里描述的是目标 skill 设计，不等于当前 helper 已经完整支持该结构

## 当前实现

当前 skill 已经提供面向 OpenClaw 编排的确定性 step 脚本：

- `run_cleanup_step.sh`
- `run_pending_rows_step.sh`
- `run_single_row_update_step.sh`
- `run_keyword_candidate_step.sh`
- `run_insert_seed_row_step.sh`

这些 step 脚本统一通过 `run_skill_step.py` 调 framework task，并复用：

- `chrome_cdp` 与 `roxy` 两类浏览器 profile 解析
- `__OPENCLAW_RESULT__ <json>` 尾行协议
- 进度日志、心跳日志和运行文件输出

仍保留但不再建议作为 v3.0 主路径使用的旧脚本：

- `run_feishu_tiktok_sync.sh`
- `run_batch_sync.sh`
- `run_cleanup.sh`

## 常见错误

### 缺少 `skill.local.env`

- 无法读取本地业务配置
- 处理方式：从 `skill.local.env.example` 复制并填写

### 缺少 `TABLE_URL`

- 无法定位飞书目标表
- 处理方式：补充正确的飞书表地址

### 缺少 `FEISHU_ACCESS_TOKEN`

- 无法读取或回写飞书数据
- 处理方式：补充有效的飞书 token

### 缺少 `FASTMOSS_PHONE` 或 `FASTMOSS_PASSWORD`

- 无法执行关键词搜索或 FastMoss 明细抓取
- 处理方式：在 `skill.local.env` 中补充 FastMoss 登录信息

### 浏览器 profile 未就绪

- 无法进入 TikTok 或 FastMoss 页面
- 处理方式：`chrome_cdp` 模式下先启动对应 CDP 端点；`roxy` 模式下检查 `ROXY_HOST`、`ROXY_TOKEN` 与 profile 配置

## 推荐调用方式

在 v3.0 下，推荐由 OpenClaw 按步骤调用：

- 定时任务：
  - `bash run_cleanup_step.sh`
  - `bash run_pending_rows_step.sh`
  - `bash run_single_row_update_step.sh --record-id <record_id>`
- 关键词搜索：
  - `bash run_keyword_candidate_step.sh --search-keyword "<keyword>" --sales-7d-threshold <number>`
  - `bash run_insert_seed_row_step.sh --sku-id <sku_id> --search-keyword "<keyword>"`
  - `bash run_single_row_update_step.sh --record-id <record_id>`
