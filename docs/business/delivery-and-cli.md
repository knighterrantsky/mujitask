# 交付与调用方式

当前这个业务仓库支持两种交付方式，可以同时保留。

## 1. HTTP 服务模式

适合给 OpenClaw 或其他外部编排系统调用。

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

适合场景：

- 客户端已经接入 OpenClaw
- 需要统一用 HTTP 协议触发任务
- 需要远程部署成常驻服务

## 2. 直接脚本模式

适合本地调试、定时任务、内部系统脚本集成，或者客户不想部署常驻 agent 服务时使用。

命令行调用：

```bash
automation-business-scaffold-run run \
  --task tiktok_product_to_feishu \
  --product-url 'https://www.tiktok.com/shop/pdp/1729440407432826887' \
  --run-mode draft
```

模块调用：

```bash
python -m automation_business_scaffold.cli run \
  --task tiktok_product_to_feishu \
  --product-url 'https://www.tiktok.com/shop/pdp/1729440407432826887'
```

Python 脚本内直接调用：

```python
from automation_business_scaffold.cli import run_registered_task

payload = run_registered_task(
    task_name="tiktok_product_to_feishu",
    params={
        "product_url": "https://www.tiktok.com/shop/pdp/1729440407432826887",
        "run_mode": "draft",
    },
)

print(payload["status"])
print(payload["run_id"])
print(payload["artifacts_dir"])
```

直接脚本模式的产物位置：

- run 记录：`runtime/cli_runs/`
- 中间步骤：`runtime/cli_runs/steps/`
- 信号：`runtime/cli_runs/signals/`
- artifacts：`runtime/artifacts/<run_id>/`

## 3. 建议的交付方案

推荐同时交付下面几项：

1. 一份可启动 HTTP 服务的部署说明，给 OpenClaw 使用
2. 一份 CLI 调用说明，给调试和兜底调用使用
3. 一个固定的 Python 环境安装命令
4. 一个 `.env` 或配置样例
5. 一份任务参数说明

当前 `tiktok_product_to_feishu` 最关键的入参是：

- `product_url`
- `run_mode`
- `trace_id`
- `field_mapping`

## 4. 什么时候选哪一种

优先选 HTTP 服务模式：

- 客户已经通过 OpenClaw 调你的服务
- 希望和现有 agent 协议完全一致

优先选直接脚本模式：

- 只是单机执行
- 想做 cron / Airflow / 本地脚本调用
- 不想维护常驻服务进程
