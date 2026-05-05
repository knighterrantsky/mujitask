# 需求池

更新时间：`2026-04-25`

状态: 需求候选池，不作为实现事实来源

## 事实来源限制

本文件只保存待确认需求和原始想法。Codex 或开发者不能直接按本文件实现业务逻辑；只有当候选项被整理并提升到 `docs/business/requirements/*.md` 后，才进入正式需求事实来源。

## 候选项索引

| item_id | title | source | affected_tables | decision_status | current_assumption | required_confirmation | promote_to | last_reviewed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| REQ-BACKLOG-001 | TK选品收集表扩展 | 2026-04-14 新增四表需求 | `TK选品收集` | promoted | 已提升为正式需求文档 | 自动采集部分已澄清并写入 `docs/business/requirements/tk-selection-collection-expand.md`；关键词搜索/店铺入口等独立选品入口仍待后续 | `docs/business/requirements/tk-selection-collection-expand.md` | 2026-04-30 |
| REQ-BACKLOG-002 | TK达人池表扩展 | 2026-04-14 新增四表需求 | `TK达人池` | partially_promoted | 一人一行，按 `达人ID` upsert；筛选为商品页达人销量 `>50` 且粉丝数 `>5000` | 后续如要求联系方式必填、自动新增店铺选项，再单独确认 | `docs/business/requirements/sync-tk-influencer-pool.md` | 2026-04-25 |
| REQ-BACKLOG-003 | TK达人建联表扩展 | 2026-04-14 新增四表需求 | `TK达人建联表` | pending_confirmation | 先按建联事件粒度理解，一行代表一次商品建联达人 | 是否新增 `达人ID`、30 天未履约起算点、监控频率、播放量获取方式 | `docs/business/requirements/*.md` | 2026-04-25 |
| REQ-BACKLOG-004 | TK合作爆款视频表扩展 | 2026-04-14 新增四表需求 | `TK合作爆款视频` | pending_confirmation | 先按商品详情页维度理解，一行代表一条满足阈值的视频 | `skuid` 真实定义、关联视频筛选范围、回写字段口径 | `docs/business/requirements/*.md` | 2026-04-25 |
| REQ-BACKLOG-005 | 紫鸟指纹浏览器支持 | 需求池记录 | 浏览器 / profile | pending_evaluation | 可能作为后续浏览器 profile provider | 接入方式、账号隔离和部署形态 | `docs/arch/*.md` | 2026-04-25 |
| REQ-BACKLOG-006 | 运行时直接下发 profile | 需求池记录 | profile / config | pending_evaluation | 调用方可在执行时传入 profile，不必须提前固化在项目配置里 | 参数边界、权限、安全和兼容策略 | `docs/arch/*.md` | 2026-04-25 |
| REQ-BACKLOG-007 | profile 和 session 文件合并 | 需求池记录 | profile / session | pending_evaluation | 可减少配置分散 | 文件格式、迁移策略、兼容策略 | `docs/arch/*.md` | 2026-04-25 |
| REQ-BACKLOG-008 | Agent 连接 server、日志上传和自动更新 | 需求池记录 | agent / server / release | pending_evaluation | agent 需要运行日志版本信息和 update 接口 | server 协议、升级权限、失败回滚、安全边界 | `docs/arch/*.md` | 2026-04-25 |
| REQ-BACKLOG-009 | OpenClaw 保存执行汇总 JSON | 需求池记录 | OpenClaw / outbox | pending_evaluation | 每次执行后本地保存 summary JSON | 保存路径、生命周期、隐私字段 | `docs/arch/*.md` | 2026-04-25 |
| REQ-BACKLOG-010 | TikTok 商品变体识别 | 需求池记录 | TikTok 商品采集 | pending_evaluation | 识别 Color、Size 等规格 | 字段映射、Fact DB schema、飞书写回目标 | `docs/business/requirements/*.md` | 2026-04-25 |

## 说明

下面的原始记录只保留来源语境，不能越过上面的候选项索引直接成为实现依据。

后续补充方式尽量简单：

- 直接写一句话或一小段话
- 只标 `P0 / P1 / P2 / P3`
- 先进入需求池，后面再决定是否进入正式需求文档和设计文档

优先级约定：

- `P0`：强阻塞，必须优先处理
- `P1`：很重要，建议尽快评估和排期
- `P2`：重要但不着急，可以放在后续版本
- `P3`：先记录，后面有需要再看

## 当前记录

- `P0`  大量Job 并行的时候 数据库连接数量过多

- `P1`  新增需求
选品收集表
数据来源 关键词搜索    店铺ID    直接店铺主页 销量排名。filter 待定
商品评论 商品评分 商品主图 商品侧边栏图片 获取方式？
出单品类占比 文字 视频 直播 商品卡 占比 以及销量 总销量暂时不写，备注 中写具体


达人池表
数据来源 通过竞品收集表中的 fastmoss 商品页面的达人信息获取 filter 销量大于50 & 粉丝数大于5000
数据逻辑 关联商品直接用竞品收集跳转过来的商品 图片，商品总销量 在竞品详情中 的对应达人总销量数据获取
增加接口  用户在飞书 通过达人ID 获取达人联系方式，成功返回ID 失败返回失败信息

达人建联表
数据入口，商品SKU 达人ID 建联时间
数据逻辑，对SKU对应的详情页下 表格中的达人的对应SKU视频进行监控，如果发布的话 在表格记录视频链接，建联时间超过30天 认定为未履约，每天定时检查
数据逻辑 根据TikTok视频链接 获取播放量
店铺合作商品数有问题

TK合作爆款视频表
数据来源 fastmoss 商品详情页面（详情页面根据客户提供的skuid） 关联视频 播放量大于20万视频

删除竞品表 之后达人数据依旧保留
达人表中粉丝数、带货视频GMV、带货直播GMV：数据库/快照中保留接口返回的实际数字；写入飞书表做最终展示时，数值大于等于 10000 的统一显示为 W 单位（例如 10000 显示为 1W，1230000 显示为 123W），小于 10000 的直接显示原始数字。
达人表粉丝数 粉丝数、带货视频GMV、带货直播GMV 大于1W 四舍五入，小于1W 直接写<1W
达人联系方式无限额度
视频的播放量和点赞量能不能看 记录一周的播放量和点赞量
选品收集表入口 增加选择条件
达人表中合作商品数不作为本期更新字段。
达人数据有更新和新建 如何区分，涉及到新的达人要进行建联 同时旧的达人数据也会有更新所以单纯用更新时间无法判断

- `P2` 支持紫鸟指纹浏览器

- `P3` 安全提醒

- `P1` 除了项目内部切换 profile，还希望支持 profile 直接下发的方式。也就是调用方在执行时直接把 profile 传进来，而不是必须提前固化在项目配置里。

- `P3` profile 文件和 session 文件可以考虑做成一个文件，减少配置分散的问题，方便保存、复制、分发和迁移。后续再评估文件格式、兼容策略和迁移方式。

- `P2` 现在的CLI形式向agent形式转换，同时agent要能够连接server 把报错的运行时记录进行打包上传 用于后续分析server端分析修复问题之后 release 成功自动调用agent的升级接口 更新软件，gent需要有接口支持update，这就意味着agent的运行日志中还需要带有对应的版本

- `P2` openclaw每次执行需要在本地保存执行汇总的json文件


- `P3` https://www.tiktok.com/shop/pdp/1730892854181139253 识别变体 Color Size 还有一些别的规格




## 后续补充方式

后面如果继续往里记，直接按这种风格追加就可以：

- `P1` 这里写一句自然语言需求，先记下来，后面再评估。
- `P2` 一个项目下的任务配置希望可以复用，不想每次都重新填一遍。
- `P3` 后续也许可以支持更细的账号隔离策略，先放进需求池。

## 转正式需求的规则

如果某条需求后面确定要做，再把它整理进正式文档：

- 进入明确交付范围后，补到 [business-requirements.md](./business-requirements.md)
- 需要设计实现方案后，补到 [../arch/README.md](../arch/README.md) 对应 workflow 或架构文档

这份文档本身只负责先把想法留住，作为后续开发评估的来源。
