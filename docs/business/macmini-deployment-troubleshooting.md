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

- 真实写入飞书时使用 `approval_required`
- 只预览字段时使用 `draft`

### 正确命令

真实写入：

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run run \
  --task tiktok_feishu_single_sync \
  --params-json '{
    "product_url": "https://www.tiktok.com/shop/pdp/1729440407432826887",
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "run_mode": "approval_required"
  }'
```

只预览：

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run run \
  --task tiktok_feishu_single_sync \
  --params-json '{
    "product_url": "https://www.tiktok.com/shop/pdp/1729440407432826887",
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "run_mode": "draft"
  }'
```

## 5. 飞书报错 `URLFieldConvFail`

### 问题现象

任务已经抓到 TikTok 数据，但在飞书新建记录时报错：

```bash
FeishuAPIError: URLFieldConvFail
```

### 原因

- `产品链接` 在飞书中是 URL 字段
- 飞书 URL 字段不能写普通字符串
- 需要写成对象结构：`{"text": "...", "link": "..."}`

### 解决方法

代码层已经修复，`source_url` 会输出为飞书 URL 字段对象。  
如果客户本地代码比较旧，需要先更新到包含该修复的版本。

### 正确结构

```json
{
  "产品链接": {
    "text": "https://www.tiktok.com/shop/pdp/1729440407432826887",
    "link": "https://www.tiktok.com/shop/pdp/1729440407432826887"
  }
}
```

## 6. 返回 `skipped_existing`

### 问题现象

命令顶层 `status` 是 `success`，但业务结果里看到：

```json
{
  "status": "skipped_existing"
}
```

### 原因

这不是失败，而是命中了去重逻辑。

系统会按下面顺序检查：

1. 先按 `产品链接` 查整张飞书表
2. URL 未命中时，再按 `SKU-ID` 查整张飞书表

命中任一条件就不会重复新建。

### 解决方法

- 如果只是验证链路，说明任务已经跑通到去重阶段
- 如果确实要再次插入，需要换一个表里没有的 TikTok URL
- 或者先手动删除飞书中已有记录后再重试

### 如何判断

- `inserted`：本次已真实新建飞书记录
- `skipped_existing`：表里已有相同 URL 或 SKU，已跳过
- `preview`：只预览，不写入

## 部署后最小验证流程

建议部署完成后按这个顺序验证：

1. 列出任务

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run list-tasks
```

2. 单条 `draft` 预览

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run run \
  --task tiktok_feishu_single_sync \
  --params-json '{
    "product_url": "https://www.tiktok.com/shop/pdp/1729440407432826887",
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "run_mode": "draft"
  }'
```

3. 单条 `approval_required` 真实写入

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run run \
  --task tiktok_feishu_single_sync \
  --params-json '{
    "product_url": "https://www.tiktok.com/shop/pdp/1729440407432826887",
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "run_mode": "approval_required"
  }'
```

4. 查看中间数据

- `run_file`
- `steps_file`
- `signals_file`
- `artifacts_dir`
