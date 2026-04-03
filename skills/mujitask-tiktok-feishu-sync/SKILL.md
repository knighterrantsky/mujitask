# mujitask-tiktok-feishu-sync

这个 skill 是可运行实例，不是说明模板。

## 作用

- 从飞书多维表格读取 TikTok 竞品记录
- 执行 `tiktok_product_link_cleanup` 做 `产品链接` 规范化和去重
- 执行 `tiktok_feishu_batch_sync` 抓取阶段一字段并回写飞书

## 本地前置

调用前必须已经完成部署，并满足：

- 当前 skill 目录下存在 `skill.local.env`
- `INSTALL_DIR` 指向已安装完成的项目目录
- 项目目录下存在 `.venv`
- 项目目录下存在 `config/browser_profiles.json`

固定规则：

- `url_field_name` 固定为 `产品链接`
- `profile_ref` 固定为 `local-chrome`

## 配置文件

本地配置文件固定为：

- `skill.local.env`

配置项只允许：

- `INSTALL_DIR`
- `TABLE_URL`
- `FEISHU_ACCESS_TOKEN`

如果缺少该文件，先从 `skill.local.env.example` 复制并填写。

## 调用入口

### 1. 链接清洗

优先在以下场景执行：

- 新表第一次接入
- 发现重复链接
- batch sync 返回 `skipped_duplicate_needs_cleanup`

macOS:

```bash
bash run_cleanup.sh draft
bash run_cleanup.sh canary
```

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_cleanup.ps1 -RunMode draft
powershell -ExecutionPolicy Bypass -File .\run_cleanup.ps1 -RunMode canary
```

### 2. 批量补录

如果需要真正抓取并写回飞书，使用 `canary`。

macOS:

```bash
bash run_batch_sync.sh draft 0
bash run_batch_sync.sh canary 0
```

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_batch_sync.ps1 -RunMode draft -MaxRecords 0
powershell -ExecutionPolicy Bypass -File .\run_batch_sync.ps1 -RunMode canary -MaxRecords 0
```

说明：

- `MaxRecords=0` 表示不限制条数
- batch sync 会自动检查 `http://127.0.0.1:9222`
- 如果本机 Chrome 已安装但 CDP 未启动，会先尝试调用 `start_browser_cdp.*`

## 常见阻塞

- 缺少 `skill.local.env`
  - 先补配置文件
- 缺少 `.venv`
  - 重新执行部署脚本
- 缺少 Chrome
  - 安装 Chrome 后重新执行部署脚本或浏览器启动脚本
- `list-tasks` 看不到正式入口
  - 部署未完成，重新执行部署脚本

## 期望返回

包装脚本直接透传 CLI JSON 输出。

重点关注：

- `status`
- `result.summary`
- `result.failed_items`
- `run_file`
- `artifacts_dir`
