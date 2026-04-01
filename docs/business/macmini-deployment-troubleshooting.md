# Mac mini 部署问题排查

这份文档记录本项目在客户 Mac mini 部署过程中实际遇到的问题、原因和解决方法。  
适用场景：

- OpenClaw 在 Mac mini 上直接调用本机 CLI
- 首次安装、更新、真实写入飞书、多 URL 批量处理

## 1. `Not a git repository: ~/apps/mujitask`

### 问题现象

执行更新脚本时报错：

```bash
Not a git repository: ~/apps/mujitask
```

### 原因

- 把 `~/apps/mujitask` 放在单引号里，shell 不会展开 `~`
- `update_local_cli.sh` 只能用于已经安装过的仓库目录

### 解决方法

- 统一使用 `"$HOME/apps/mujitask"`，不要写成 `'~/apps/mujitask'`
- 如果还没有安装过仓库，先执行 `install_local_cli.sh`

### 正确命令

首次安装：

```bash
curl -fsSL \
  'https://raw.githubusercontent.com/knighterrantsky/mujitask/<release-tag>/examples/macmini/install_local_cli.sh' \
  | bash -s -- \
    'https://github.com/knighterrantsky/mujitask.git' \
    "$HOME/apps/mujitask" \
    '<release-tag>'
```

已有仓库时更新：

```bash
curl -fsSL \
  'https://raw.githubusercontent.com/knighterrantsky/mujitask/<release-tag>/examples/macmini/update_local_cli.sh' \
  | bash -s -- \
    "$HOME/apps/mujitask" \
    '<release-tag>'
```

## 2. `uv is required but not installed`

### 问题现象

执行安装或更新脚本时报错：

```bash
uv is required but not installed
```

### 原因

客户 Mac mini 没有安装 `uv`。

### 解决方法

先安装 `uv`，再重新运行安装或更新脚本。

### 正确命令

官方安装器：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.zshrc
uv --version
```

如果机器已安装 Homebrew，也可以：

```bash
brew install uv
uv --version
```

## 3. `uv sync` 拉取 framework 失败，GitHub 凭证错误

### 问题现象

安装或更新时，`uv sync` 报类似错误：

```bash
fatal: could not read Username for 'https://github.com': terminal prompts disabled
```

### 原因

- `automation-framework` 依赖是通过 GitHub 拉取的
- 当前机器没有预先配置可用的 GitHub 凭证
- 脚本是非交互执行，不能临时弹出登录框

### 解决方法

在 Mac mini 上先配置 GitHub 访问凭证，再执行安装或更新。

可选方案：

- `gh auth login`
- Personal Access Token
- SSH key

### 正确命令

如果使用 GitHub CLI：

```bash
gh auth login
gh auth setup-git
git ls-remote https://github.com/knighterrantsky/automation-framework.git
```

如果使用 SSH：

```bash
ssh-keygen -t ed25519 -C "macmini-mujitask"
ssh -T git@github.com
git config --global url."git@github.com:".insteadOf "https://github.com/"
```

验证通过后再执行：

```bash
cd "$HOME/apps/mujitask"
bash examples/macmini/update_local_cli.sh "$HOME/apps/mujitask" '<release-tag>'
```

## 4. `run_mode=live` 导致 `WorkflowSpec` 校验失败

### 问题现象

执行单条或批量任务时报错，核心信息类似：

```bash
ValidationError: 1 validation error for WorkflowSpec
run_mode
Input should be 'observe', 'draft', 'approval_required', 'canary' or 'full_auto'
```

### 原因

- framework 合法的 `run_mode` 枚举里没有 `live`
- `live` 只是业务口头语义，不是 framework 接口字段

### 解决方法

- 真实写入飞书时使用 `canary`
- 只预览字段时使用 `draft`

### 正确命令

真实写入：

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run run \
  --task tiktok_product_link_cleanup \
  --params-json '{
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "url_field_name": "产品链接",
    "run_mode": "canary"
  }'
```

只预览：

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run run \
  --task tiktok_feishu_batch_sync \
  --params-json '{
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "url_field_name": "产品链接",
    "profile_ref": "local-chrome",
    "run_mode": "draft"
  }'
```

## 5. 部署后最小验证流程

建议部署完成后按这个顺序验证：

1. 列出任务

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run list-tasks
```

2. cleanup `draft` 预览

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run run \
  --task tiktok_product_link_cleanup \
  --params-json '{
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "url_field_name": "产品链接",
    "run_mode": "draft"
  }'
```

3. 阶段一 `draft` 预览

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run run \
  --task tiktok_feishu_batch_sync \
  --params-json '{
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "url_field_name": "产品链接",
    "profile_ref": "local-chrome",
    "run_mode": "draft"
  }'
```

4. 阶段一 `canary` 真实写回

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run run \
  --task tiktok_feishu_batch_sync \
  --params-json '{
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "url_field_name": "产品链接",
    "profile_ref": "local-chrome",
    "run_mode": "canary"
  }'
```

5. 查看中间数据

- `run_file`
- `steps_file`
- `signals_file`
- `artifacts_dir`
