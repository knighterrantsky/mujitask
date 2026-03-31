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

### 单条 URL

```bash
automation-business-scaffold-run run \
  --task tiktok_feishu_single_sync \
  --params-json '{
    "product_url": "https://www.tiktok.com/shop/pdp/1729440407432826887",
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "run_mode": "live"
  }'
```

### 多 URL 顺序处理

```bash
automation-business-scaffold-run run \
  --task tiktok_feishu_batch_sync \
  --params-json '{
    "product_urls": [
      "https://www.tiktok.com/shop/pdp/1729440407432826887",
      "https://www.tiktok.com/shop/pdp/1729732615040962895"
    ],
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "run_mode": "live"
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
    task_name="tiktok_feishu_single_sync",
    params={
        "product_url": "https://www.tiktok.com/shop/pdp/1729440407432826887",
        "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
        "access_token_env": "FEISHU_ACCESS_TOKEN",
        "run_mode": "live",
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
4. 多 URL 的标准调用示例
5. 返回字段和排障路径说明

单条正式 task 的关键入参：

- `product_url`
- `table_url`
- `access_token_env`
- `run_mode`
- `trace_id`
- `field_mapping`

批量正式 task 的关键入参：

- `product_urls`
- `table_url`
- `access_token_env`
- `run_mode`
- `trace_id`
- `field_mapping`
- `step_delay_sec`
- `step_delay_jitter_sec`
- `record_delay_sec`
- `record_delay_jitter_sec`
- `pause_every`
- `pause_sec`
- `continue_on_error`

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

单条任务的 `result.data` 重点字段：

- `status`
- `record_id`
- `product_url`
- `product_id`
- `fields`

批量任务的 `result.data` 重点字段：

- `summary`
- `items`
- `settings`

## 5. 什么时候选哪一种

优先选 HTTP 服务模式：

- 客户已经统一接入 agent 协议
- 确实需要远程常驻服务

优先选 CLI 模式：

- 客户环境是单机部署
- OpenClaw 直接调本机命令
- 想要更直接的同步返回和更容易排障的中间数据
