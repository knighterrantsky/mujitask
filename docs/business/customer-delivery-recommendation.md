# 客户交付建议与部署要求

这份文档用于和客户对齐当前 TikTok → 飞书采集服务的交付方式、部署要求、调用方式和排障入口。

## 1. 推荐交付形态

当前推荐采用：

- 客户 Mac mini 本机部署
- OpenClaw 通过 skill 直接调用本机 CLI
- 命令同步执行并返回 JSON 结果

不建议当前阶段优先采用：

- 先部署成常驻 HTTP agent 服务
- 把飞书 token 直接写进 skill 文本
- 把“飞书待处理表格回写”当成正式主链路

推荐原因：

- 和客户现有 OpenClaw 使用方式一致
- 部署更轻，维护更简单
- 返回结果清晰，OpenClaw 更容易判断成功、跳过或失败
- 所有后续自动化业务都可以继续放在同一个工程里演进

## 2. 客户环境要求

客户机器需要具备：

- `git`
- `uv`
- Python `3.11`
- 可以访问 TikTok 和飞书的网络环境

推荐部署目录：

- `~/apps/mujitask`

标准仓库地址：

- 业务仓库：`https://github.com/knighterrantsky/mujitask.git`
- framework：`https://github.com/knighterrantsky/automation-framework.git`

更详细的本机部署说明见：

- [macmini-deployment.md](/Users/happyzhao/Work/mujitask/docs/business/macmini-deployment.md#L1)

## 3. OpenClaw 正式调用能力

当前对外正式说明两个 task：

- `tiktok_feishu_single_sync`
- `tiktok_feishu_batch_sync`

说明：

- `tiktok_feishu_single_sync`：输入 1 个 TikTok URL，抓取后新建 1 条飞书记录
- `tiktok_feishu_batch_sync`：输入一组 TikTok URLs，顺序重复单条插入链路
- `tiktok_product_to_feishu`：保留为底层调试 task，只负责字段构建，不作为客户 skill 主入口

### 单条 URL 调用

```bash
cd ~/apps/mujitask
.venv/bin/automation-business-scaffold-run run \
  --task tiktok_feishu_single_sync \
  --params-json '{
    "product_url": "https://www.tiktok.com/shop/pdp/1729440407432826887",
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "run_mode": "approval_required"
  }'
```

### 多 URL 批量调用

```bash
cd ~/apps/mujitask
.venv/bin/automation-business-scaffold-run run \
  --task tiktok_feishu_batch_sync \
  --params-json '{
    "product_urls": [
      "https://www.tiktok.com/shop/pdp/1729440407432826887",
      "https://www.tiktok.com/shop/pdp/1729732615040962895"
    ],
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "run_mode": "approval_required"
  }'
```

## 4. 返回结构约定

CLI 顶层固定返回：

- `status`
- `run_id`
- `result`
- `error`
- `run_file`
- `steps_file`
- `signals_file`
- `artifacts_dir`

OpenClaw 建议按这个顺序判断：

1. 看顶层 `status`
2. 如果失败，读取顶层 `error`
3. 如果成功，读取 `result.data`

单条任务的 `result.data` 主结构：

- `status`
- `record_id`
- `product_url`
- `product_id`
- `fields`

其中 `status` 可能是：

- `inserted`
- `skipped_existing`
- `preview`

批量任务的 `result.data` 主结构：

- `summary`
- `items`
- `settings`

其中 `summary` 至少包含：

- `total`
- `processed`
- `inserted`
- `skipped_existing`
- `previewed`
- `failed`

## 5. 去重策略

正式链路固定按“存在则跳过”执行：

1. 先按 `产品链接` 查整张飞书表
2. URL 不存在时，抓到商品后再按 `SKU-ID` 查整张飞书表
3. 命中后返回 `skipped_existing`
4. 不更新原记录，也不重复新建

这套策略同时适用于单条和批量 URL 模式。

## 6. 延迟与节流

默认节流参数：

- `step_delay_sec = 1.0`
- `step_delay_jitter_sec = 1.0`
- `record_delay_sec = 2.0`
- `record_delay_jitter_sec = 2.0`
- `pause_every = 5`
- `pause_sec = 8.0`
- `continue_on_error = true`

节流逻辑：

- 单条任务内，每个外部步骤之间加随机 delay
- 批量任务里，每条记录之间继续加随机 delay
- 单条失败默认不影响后续条目

## 7. 配置提供方式

推荐分成两层：

1. 机密配置：环境变量
2. 调用参数：OpenClaw 直接传参

建议约定：

- `FEISHU_ACCESS_TOKEN`：通过环境变量提供
- `table_url`：每次调用显式传入
- `field_mapping`：如需覆盖列名时显式传入

示例配置文件保留在：

- [customer.local.example.json](/Users/happyzhao/Work/mujitask/examples/macmini/customer.local.example.json#L1)

注意：

- 当前 CLI 不会自动读取这个 JSON 文件
- 这个文件适合作为客户本地样例，不是正式调用入口
- 不要把 token 写进 git，也不要写进 skill markdown

## 8. 排障入口

运行中间数据默认落在：

- `runtime/cli_runs/`
- `runtime/cli_runs/steps/`
- `runtime/cli_runs/signals/`
- `runtime/artifacts/<run_id>/`

推荐排障顺序：

1. 查看 `run_file`
2. 查看 `steps_file`
3. 查看 `signals_file`
4. 查看 `artifacts_dir` 中的 `state_dump` 和下载图片

## 9. Skill 交付建议

交付给 OpenClaw 时，建议同时提供：

1. 一份固定安装命令
2. 一份固定更新命令
3. 单条 URL 的标准调用示例
4. 多 URL 的标准调用示例
5. 返回字段判断规则
6. 排障路径说明

可直接复制给 OpenClaw 的 skill 模板见：

- [openclaw-skill-template.md](/Users/happyzhao/Work/mujitask/docs/business/openclaw-skill-template.md#L1)
