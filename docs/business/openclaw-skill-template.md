# OpenClaw Skill 模板

下面这份内容可以作为 OpenClaw 的中文 skill 模板直接使用。

---

## Skill 名称

TikTok 飞书表格清洗与阶段一补录

## Skill 目标

这个 skill 用于在客户本机调用 `mujitask` CLI，完成两类表驱动操作：

- `tiktok_product_link_cleanup`：读取飞书表中的 `产品链接`，格式化后回写，并删除重复整行
- `tiktok_feishu_batch_sync`：读取飞书表中的现有记录，只对阶段一字段存在空缺的记录执行浏览器抓取和回写

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

- `tiktok_product_link_cleanup`
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

- `tiktok_product_link_cleanup`
- `tiktok_feishu_batch_sync`

`tiktok_product_to_feishu` 只是底层调试 task，不作为主入口。

### 1. 链接整理 / 去重

适用场景：

- 飞书表中已有一批原始 TikTok 商品链接
- 需要统一格式化 `产品链接`
- 需要基于规范 URL 去重并删除重复整行

命令模板：

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run run \
  --task tiktok_product_link_cleanup \
  --params-json '{
    "table_url": "<feishu_table_url>",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "url_field_name": "产品链接",
    "run_mode": "canary"
  }'
```

可选参数：

- `trace_id`
- `url_field_name`

### 2. 阶段一表格补录

适用场景：

- 飞书表中已经有经过 cleanup 的记录
- 需要补齐阶段一字段：`SKU-ID / 图片 / 标题 / 节日 / 卖家 / 前台截图 / 价格 / 记录日期`
- 只补写当前空缺字段
- 只要发生写回，就同步刷新 `记录日期`

命令模板：

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run run \
  --task tiktok_feishu_batch_sync \
  --params-json '{
    "table_url": "<feishu_table_url>",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "url_field_name": "产品链接",
    "profile_ref": "local-chrome",
    "run_mode": "canary"
  }'
```

可选参数：

- `trace_id`
- `record_delay_sec`
- `record_delay_jitter_sec`
- `pause_every`
- `pause_sec`
- `max_records`
- `retry_attempts`
- `retry_delay_sec`

默认节流值：

- `step_delay_sec = 1.0`
- `step_delay_jitter_sec = 1.0`
- `record_delay_sec = 2.0`
- `record_delay_jitter_sec = 2.0`
- `pause_every = 5`
- `pause_sec = 8.0`
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

cleanup 任务 `result.data` 重点字段：

- `summary`
- `items`
- `settings`

cleanup 典型状态：

- `preview`
- `updated`
- `delete_preview`
- `deleted`
- `invalid_url`
- `skipped_empty`

阶段一任务 `result.data` 重点字段：

- `summary`
- `items`
- `failed_items`
- `settings`

阶段一任务 `summary` 重点字段：

- `total`
- `updated`
- `skipped_completed`
- `skipped_not_cleaned`
- `skipped_duplicate_needs_cleanup`
- `failed`

阶段一处理规则：

1. 只读取当前飞书表 / 视图中的记录
2. 只有 `产品链接` 已经规范化，才允许进入阶段一
3. 如果同一规范 URL 还有重复记录，返回 `skipped_duplicate_needs_cleanup`
4. 阶段一字段全部有值时，返回 `skipped_completed`
5. 只要阶段一字段存在空缺，就执行“读一条、抓一条、写一条”
6. 只补当前缺失字段，但只要写回就必须同时更新 `记录日期`

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
- 如果没有 `table_url`，先提示缺少飞书表格地址
- 正式顺序是先 `tiktok_product_link_cleanup`，再 `tiktok_feishu_batch_sync`
- `tiktok_feishu_batch_sync` 不是吃 `product_urls[]` 的接口，而是飞书表驱动入口

---

上面这份模板可以直接复制到 OpenClaw 的 skill 配置里使用。
