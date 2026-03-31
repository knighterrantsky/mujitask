# OpenClaw Skill 模板

下面这份内容可以作为 OpenClaw 的中文 skill 模板直接使用。

---

## Skill 名称

TikTok 商品抓取并插入飞书多维表格

## Skill 目标

这个 skill 用于在客户本机调用 `mujitask` CLI，完成两类操作：

- 单条 URL：抓取 1 个 TikTok 商品链接并插入飞书 1 条记录
- 多 URL 批量：顺序处理一组 TikTok 商品链接，逐条插入飞书记录

不要把真实的飞书 token 写进 skill 文本，token 只能通过客户机器的环境变量提供。

## 本地环境要求

客户机器必须具备：

- `git`
- `uv`
- Python `3.11`
- 能访问 TikTok 和飞书的网络

默认安装目录：

- `$HOME/apps/mujitask`

部署常见问题排查文档：

- [macmini-deployment-troubleshooting.md](/Users/happyzhao/Work/mujitask/docs/business/macmini-deployment-troubleshooting.md)

业务仓库：

- `https://github.com/knighterrantsky/mujitask.git`

framework 仓库：

- `https://github.com/knighterrantsky/automation-framework.git`

## 首次安装命令

优先使用固定 tag 安装：

```bash
curl -fsSL \
  'https://raw.githubusercontent.com/knighterrantsky/mujitask/<release-tag>/examples/macmini/install_local_cli.sh' \
  | bash -s -- \
    'https://github.com/knighterrantsky/mujitask.git' \
    "$HOME/apps/mujitask" \
    '<release-tag>'
```

安装后可用下面命令验证：

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run list-tasks
```

预期至少能看到：

- `tiktok_feishu_single_sync`
- `tiktok_feishu_batch_sync`

## 更新命令

```bash
curl -fsSL \
  'https://raw.githubusercontent.com/knighterrantsky/mujitask/<release-tag>/examples/macmini/update_local_cli.sh' \
  | bash -s -- \
    "$HOME/apps/mujitask" \
    '<release-tag>'
```

## 环境变量要求

客户机器必须提前设置：

```bash
export FEISHU_ACCESS_TOKEN='your-feishu-access-token'
```

skill 不要接收真实 token 文本，也不要把 token 回显到回答里。

## 正式调用入口

正式只使用下面两个 task：

- `tiktok_feishu_single_sync`
- `tiktok_feishu_batch_sync`

`tiktok_product_to_feishu` 只是底层调试 task，不作为主入口。

### 1. 单条 URL 调用

适用场景：

- 用户给出 1 个 TikTok 商品链接
- 需要抓取后插入飞书 1 条记录

命令模板：

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run run \
  --task tiktok_feishu_single_sync \
  --params-json '{
    "product_url": "<tiktok_product_url>",
    "table_url": "<feishu_table_url>",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "run_mode": "approval_required"
  }'
```

可选参数：

- `trace_id`
- `field_mapping`
- `step_delay_sec`
- `step_delay_jitter_sec`

### 2. 多 URL 批量调用

适用场景：

- 用户一次给出 10 到 20 个 TikTok 商品链接
- 需要顺序处理，并且每条记录之间带随机 delay

命令模板：

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run run \
  --task tiktok_feishu_batch_sync \
  --params-json '{
    "product_urls": [
      "<url_1>",
      "<url_2>"
    ],
    "table_url": "<feishu_table_url>",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "run_mode": "approval_required"
  }'
```

可选参数：

- `trace_id`
- `field_mapping`
- `step_delay_sec`
- `step_delay_jitter_sec`
- `record_delay_sec`
- `record_delay_jitter_sec`
- `pause_every`
- `pause_sec`
- `continue_on_error`

默认节流值：

- `step_delay_sec = 1.0`
- `step_delay_jitter_sec = 1.0`
- `record_delay_sec = 2.0`
- `record_delay_jitter_sec = 2.0`
- `pause_every = 5`
- `pause_sec = 8.0`
- `continue_on_error = true`

## 返回字段说明

CLI 顶层固定返回：

- `status`
- `run_id`
- `result`
- `error`
- `run_file`
- `steps_file`
- `signals_file`
- `artifacts_dir`

判断规则：

- 顶层 `status = success` 才算命令成功
- 顶层 `status = failed` 时读取顶层 `error`
- 顶层成功后，再看 `result.data`

单条任务 `result.data` 重点字段：

- `status`
- `record_id`
- `product_url`
- `product_id`
- `fields`

单条任务 `result.data.status` 可能取值：

- `inserted`：已新建飞书记录
- `skipped_existing`：飞书表中已存在相同 URL 或 SKU，已跳过
- `preview`：只预览字段，没有真正写入

批量任务 `result.data` 重点字段：

- `summary`
- `items`
- `settings`

批量任务 `summary` 重点字段：

- `total`
- `processed`
- `inserted`
- `skipped_existing`
- `previewed`
- `failed`

## 去重规则

固定按下面顺序去重：

1. 先按 `产品链接` 查整张飞书表
2. 如果 URL 没命中，再抓取商品并按 `SKU-ID` 查整张飞书表
3. 命中任一条件就返回 `skipped_existing`
4. 不更新旧记录，也不重复新建

## 排障入口

如果命令失败或结果异常，优先查看：

- `runtime/cli_runs/`
- `runtime/cli_runs/steps/`
- `runtime/cli_runs/signals/`
- `runtime/artifacts/<run_id>/`

排障建议：

1. 先看 `run_file`
2. 再看 `steps_file`
3. 再看 `signals_file`
4. 最后看 `artifacts_dir` 中的 `state_dump` 和下载图片

部署期常见错误与修复方法见：

- [macmini-deployment-troubleshooting.md](/Users/happyzhao/Work/mujitask/docs/business/macmini-deployment-troubleshooting.md)

## 调用约束

- 不要臆造 token
- 不要把 token 传到 `params-json`
- 不要调用 `tiktok_product_to_feishu` 作为正式业务入口
- 如果用户只给 1 个 URL，用单条 task
- 如果用户给多个 URL，用批量 task，并把它们放入 `product_urls` 数组
- 如果没有 `table_url`，先提示缺少飞书表格地址

---

上面这份模板可以直接复制到 OpenClaw 的 skill 配置里使用。
