# 交付与调用方式

当前这个业务仓库支持两种交付方式：

- HTTP 服务模式
- 本机 CLI 模式

客户当前推荐主路径是本机 CLI 模式，OpenClaw 直接调用命令即可。

## 1. HTTP 服务模式

适合已经接入统一 agent 协议、并且愿意维护常驻服务进程的场景。

启动方式：

```bash
uvicorn automation_business_scaffold.agent:app --app-dir src --host 127.0.0.1 --port 8110
```

对外接口：

- `GET /tasks`
- `POST /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/steps`
- `GET /runs/{run_id}/signals`
- `GET /runs/{run_id}/artifacts`

## 2. 本机 CLI 模式

适合 OpenClaw、本地调试、定时任务和单机脚本集成。

### 先做链接清洗

```bash
automation-business-scaffold-run run \
  --task tiktok_product_link_cleanup \
  --params-json '{
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "url_field_name": "产品链接",
    "run_mode": "canary"
  }'
```

### 再做表格驱动批量同步

```bash
automation-business-scaffold-run run \
  --task tiktok_feishu_batch_sync \
  --params-json '{
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "url_field_name": "产品链接",
    "profile_ref": "local-chrome",
    "run_mode": "canary"
  }'
```

### 底层字段调试

```bash
automation-business-scaffold-run run \
  --task tiktok_product_to_feishu \
  --product-url 'https://www.tiktok.com/shop/pdp/1729440407432826887' \
  --run-mode draft
```

Python 脚本内直接调用：

```python
from automation_business_scaffold.cli import run_registered_task

payload = run_registered_task(
    task_name="tiktok_feishu_batch_sync",
    params={
        "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
        "access_token_env": "FEISHU_ACCESS_TOKEN",
        "url_field_name": "产品链接",
        "profile_ref": "local-chrome",
        "run_mode": "canary",
    },
)

print(payload["status"])
print(payload["run_id"])
print(payload["artifacts_dir"])
```

CLI 产物位置：

- run 记录：`runtime/cli_runs/`
- 中间步骤：`runtime/cli_runs/steps/`
- 信号：`runtime/cli_runs/signals/`
- artifacts：`runtime/artifacts/<run_id>/`

## 3. 建议的交付内容

推荐同时交付下面几项：

1. 一份 Mac mini 安装/更新说明
2. 一份 OpenClaw skill 模板
3. 单条 URL 的标准调用示例
4. 表格 cleanup + batch sync 的标准调用示例
5. 返回字段和排障路径说明

cleanup task 的关键入参：

- `table_url`
- `access_token_env`
- `url_field_name`
- `run_mode`

批量正式 task 的关键入参：

- `table_url`
- `access_token_env`
- `url_field_name`
- `profile_ref`
- `run_mode`
- `trace_id`
- `record_delay_sec`
- `record_delay_jitter_sec`
- `pause_every`
- `pause_sec`
- `max_records`
- `retry_attempts`
- `retry_delay_sec`

## 4. 返回字段

CLI 顶层固定返回：

- `status`
- `run_id`
- `result`
- `error`
- `run_file`
- `steps_file`
- `signals_file`
- `artifacts_dir`

cleanup 任务的 `result.data` 重点字段：

- `summary`
- `items`
- `settings`

cleanup 说明：

- 只回写 `产品链接`
- 只删除重复整行
- 不写 `标准产品链接 / 链接整理状态 / 删除重复数`

批量任务的 `result.data` 重点字段：

- `summary`
- `items`
- `failed_items`
- `settings`

批量任务说明：

- 不是 `product_urls[]` 批量插入接口
- 是飞书表驱动的阶段一补录入口
- 逐条执行“抓取 -> 上传附件 -> 写回当前行”
- 只补空缺字段
- 只要发生写回，就同步更新 `记录日期`

## 5. 什么时候选哪一种

优先选 HTTP 服务模式：

- 客户已经统一接入 agent 协议
- 确实需要远程常驻服务

优先选 CLI 模式：

- 客户环境是单机部署
- OpenClaw 直接调本机命令
- 想要更直接的同步返回和更容易排障的中间数据
