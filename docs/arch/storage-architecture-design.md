# Storage 架构设计

日期: 2026-07-23

状态: 已实现并受机器契约与 completion gate 约束

机器契约: `contracts/facts/durable-business-object-storage.yaml`

## 1. 定位

Storage 分成两个明确边界:

| 边界 | 内容 | 存储位置 | 是否允许成为跨进程依赖 |
| --- | --- | --- | --- |
| 本地临时与运行文件 | 下载文件、转换中间文件、日志、stdout、state dump、普通截图、HTML、page-data、调试快照 | OS 临时目录或本地 `artifact_root` | 否 |
| 长期业务对象 | 已进入业务事实、业务关系或受控业务审计记录的媒体、标准化快照和证据 | MinIO | 是 |

MinIO 暂时只承担长期业务对象存储，不再承担通用 Runtime artifact、日志或调试文件归档。

核心原则:

> MinIO 默认拒绝写入。只有机器契约白名单中的长期业务对象，才能由既有 owner 在校验后上传。

这项设计同时约束 TikTok / FastMoss 与 Amazon 流程。不能因为文件已经下载到本地、体积较大、
需要跨 stage 传递或当前配置了 bucket，就自动把它提升为 MinIO 对象。

## 2. 长期业务对象判定

一个文件只有同时满足以下条件，才是长期业务对象:

1. 有稳定的业务 owner。
2. 有明确的 Fact DB 主体、关系、业务记录或受控业务审计记录绑定。
3. 原进程退出、本地临时文件删除后，业务仍需要读取该对象。
4. 有明确的保留和清理策略。
5. 属于机器契约显式允许的对象类型。

“排障可能有用”“文件比较大”“已经生成 object key”都不能单独构成持久化理由。

### 2.1 允许进入 MinIO

| 平台 | 对象类型 | 业务绑定 | 写入 owner |
| --- | --- | --- | --- |
| TikTok | 商品主图、图库、SKU 图 | `tk_media_assets` / `tk_entity_media_assets` | `media_asset_sync` |
| TikTok | 达人头像 | `tk_media_assets` / `tk_entity_media_assets` | `media_asset_sync` |
| TikTok | 视频封面 | `tk_media_assets` / `tk_entity_media_assets` | `media_asset_sync` |
| TikTok | 被正式字段或业务记录契约声明为需长期保留的图表/附件 | 对应 Fact 或业务记录附件 | `media_asset_sync` |
| Amazon | 商品主图、图库 | `amazon_media_assets` / `amazon_product_media_assets` | `media_asset_sync` |
| Amazon | `normalized_capture` | `amazon_raw_captures`，也是跨 Browser/API 进程的 Fact 输入 | `amazon_browser_capture` |
| Amazon | blocked/captcha/access-blocked 截图 | 受控终态业务审计证据 | `amazon_browser_capture` |

图表或附件只有被正式业务字段/事实契约声明为长期资产时才允许上传。一次性生成后立即写入飞书、
且不需要在 Mujitask 中长期复用的文件，仍是本地临时文件。

### 2.2 禁止进入 MinIO

- `run.json`、`steps.json`、`signals.json`。
- stdout/stderr、业务日志、阶段耗时和 progress 诊断。
- state dump、浏览器状态快照和普通/成功截图。
- 成功、失败或 blocked 页面的 HTML。
- `page-data`、`network_data` 和通用 raw API response。
- 飞书整表 raw snapshot、原始字段 envelope 和附件 envelope 快照。
- Cookie、浏览器 profile、session 导出。
- 临时下载文件、压缩/解压文件和格式转换中间文件。

禁止项只能短期保留在本地，不能因为 `artifact_store_provider=minio`、
`sync_referenced_files=true` 或 result payload 中出现本地路径而被批量同步到 MinIO。

## 3. 本地临时文件策略

所有流程都可以把外部文件下载到本地临时目录，并在本地完成解码、压缩、转码、摘要计算和飞书上传。

本地文件遵守以下约束:

- `local_path` / `source_path` 只是当前进程或当前主机的临时定位。
- 不写入 Fact DB 作为长期定位，也不作为跨进程 Runtime result 的可用性承诺。
- 原进程退出后允许不存在；下游不得依赖它仍然可读。
- Runtime DB 可以保存短期本地 artifact 索引用于当次排障，但该索引不构成业务事实。
- 本地文件由使用方或定期本地清理任务 best-effort 删除；清理失败不改变业务事实。

如果文件需要长期持久化，必须经过显式提升:

```text
本地下载/生成
→ 校验业务身份、类型和大小
→ 匹配长期业务对象白名单
→ 上传配置的 MinIO bucket
→ stat/read 远端实际字节并校验 SHA-256
→ 持久化完整对象引用
→ 本地临时文件可删除
```

不存在“先持久化 `local_path`，以后再补对象存储”的成功状态。

## 4. 持久对象引用 Contract

长期业务对象的权威引用至少包含:

```text
bucket + object_key + content_digest
```

其中 `content_digest` 是实际存储字节的 SHA-256，格式固定为 `64 位小写十六进制`。`size_bytes`、`content_type`、`file_name`、
`source_url` 和 `remote_uri` 可以作为已校验元数据，但不能替代上述三个字段。

规则:

- 上传方必须把配置中实际使用的 `bucket` 写入对象引用，不能只返回 `object_key`。
- 写入和读取都必须校验 `bucket` 精确等于当次项目配置允许的 bucket，`object_key` 命中该对象类别的专用 prefix/template；不能只验证对象存在。
- `local_path`、`source_path`、`file://` URI 和临时目录都不是持久引用字段。
- Fact DB 只有在远端对象可读、摘要匹配后才能提交对应媒体或 capture 事实。
- 下游消费者必须按完整 MinIO 引用读取，不能优先读取旧 `local_path`，也不能在本地文件失效后才 fallback MinIO。
- 引用缺少 `bucket`、`object_key` 或 `content_digest` 时，一律视为无效缓存并从业务源重新物化。
- 读取方不得用全局默认 bucket 静默猜测缺失字段，也不得修复、回填或迁移不完整旧引用。

这条规则直接避免“对象实际存在，但转换链路只保留 `object_key`、丢失 `bucket`，随后因临时文件消失而失败”的问题。

## 5. Bucket 与 Object key

首期继续复用当前配置的单个 `artifact_bucket`，不新建 bucket。`artifact_bucket` 是历史配置名；
本设计生效后，它在生产中只承载允许的长期业务对象。worker 不得在运行路径创建 bucket。

允许的 key 族:

| 对象类型 | Object key |
| --- | --- |
| TikTok 商品媒体 | `<env>/product-media/<product_id>/<media_role>-<digest>-<filename>` |
| TikTok 达人头像 | `<env>/creator-media/<creator_key>/<media_role>-<digest>-<filename>` |
| TikTok 视频封面 | `<env>/video-media/<video_key>/<media_role>-<digest>-<filename>` |
| TikTok 受控业务附件 | `<env>/business-attachments/tiktok/<business_key>/<sha256>/<filename>` |
| Amazon 标准化 capture | `<env>/raw-captures/amazon/us/<asin>/<yyyy>/<mm>/<dd>/<run_id>/<sha256>/normalized.json` |
| Amazon blocked 截图 | `<env>/raw-captures/amazon/us/<asin>/<yyyy>/<mm>/<dd>/<run_id>/<sha256>/page.png` |
| Amazon 商品媒体 | `<env>/product-media/amazon/us/<asin>/<media_role>/<sha256>.<ext>` |

`raw-captures` 是当前 Amazon capture 的既定 prefix 名称，不代表可以写任意 raw 文件。新写入只允许
Amazon `normalized_capture` 和满足终态条件的 blocked 截图。以下 key 族不得新增:

```text
<env>/runs/...
<env>/raw-html/...
<env>/raw/...generic-response...
<env>/temp/...
```

对象 key 不包含 URL query、Cookie、token、用户输入原文、浏览器 profile id、飞书账号或其他凭证。

## 6. Runtime、Fact 与 MinIO 的关系

### 6.1 Runtime DB

Runtime DB 负责 task、execution、job、lease、retry、outbox 和紧凑运行结果。
`artifact_object` 可以索引本地诊断文件，也可以索引被 Runtime 生成且符合白名单的业务对象，
但“存在 artifact_object 记录”本身不能放宽 MinIO 白名单。

Runtime 文件不能成为后续 Job 的业务输入。如果数据必须跨进程、重试或 worker 重启继续使用:

- 小型结构化数据进入受控 Runtime result 或 Fact DB；或
- 字节对象先被归类为机器契约允许的长期业务对象，再以完整 MinIO 引用交接。

不能通过把任意文件上传 MinIO 来绕开 Runtime payload 边界。

### 6.2 Fact DB

Fact DB 保存业务事实和长期对象引用，不保存媒体 body、HTML、截图 body 或临时路径。

- TikTok/FastMoss 媒体事实必须保存完整 `bucket + object_key + content_digest`。
- Amazon 商品媒体遵守同一规则，并继续执行当前的对象 size/digest 校验与源站 freshness 重验证。
- Amazon `normalized_capture` 是被允许的长期标准化业务快照。Fact handler 必须从 MinIO 读取实际字节、验证摘要与 capture contract 后写入事实。
- 通用 raw response 不因体积变大自动进入 MinIO。需要长期分析的字段应标准化到主体、关系、observation/latest 或专用受控 snapshot；其余只作短期本地诊断。
- 飞书 raw table snapshot 不是长期业务对象。Runtime 只传递已裁剪的结构化行输入，不保存或上传完整 raw snapshot。

### 6.3 `requires_object_storage`

正式 workflow 的 `requires_object_storage=true` 表示该流程会产生或读取白名单中的长期业务对象，
不是要求把所有 Runtime artifact 上传 MinIO。

需要持久媒体或 Amazon normalized capture 的正式流程，在 MinIO provider、bucket 或 credential
缺失时必须 fail fast。只产生本地日志和临时文件的流程不应仅因 MinIO 不可用而失败。

## 7. TikTok / FastMoss 统一约束

- `media_asset_sync` 是长期业务媒体的唯一通用物化 owner。
- Fact 缓存复用必须返回完整持久引用；只存在 `object_key`、`remote_uri` 或旧 `local_path` 不算命中。
- 业务 mapper/projection 只能传递已校验引用，不能自行上传文件或补猜 bucket。
- TikTok browser fallback 的 HTML、截图、state 和调试输出只保留本地。
- FastMoss/TikTok raw response 可以按受控尺寸写入 Fact DB 的结构化 raw/preview；不得自动上传 MinIO。
- 飞书附件读取长期业务对象时直接使用 MinIO 引用。为了当前调用方便下载出的临时文件，不得回写为事实定位。

## 8. Amazon 统一约束

### 8.1 成功采集

成功或 partial success 的持久对象只包括:

- `normalized_capture`。
- 已校验并实际物化的商品媒体。

HTML、page-data、network data 和成功截图可以在 Browser 进程本地用于解析或短期排障，但不能上传
MinIO，也不能成为 Fact persistence 的成功前置条件。Browser/API 之间只传
`normalized_capture_ref` 和受控媒体来源引用。

### 8.2 Blocked / captcha

`blocked`、`captcha_required` 或 `access_blocked` 在页面已经形成可验证证据时，必须保存一张受控
截图作为长期业务审计证据。该例外只允许终态截图，不允许同时持久化 HTML、page-data、
network data、state dump、普通调试截图、`normalized_capture` 或商品媒体。

预导航配置失败、DNS/连接失败等尚未形成页面证据的错误，不强制生成截图。

### 8.3 缓存复用

Amazon 媒体缓存仍使用已确认的严格规则:

- URL digest 只用于查找候选，不证明源站内容仍然相同。
- 候选对象必须属于当前环境的 Amazon prefix，并通过 MinIO 实际字节的 size 和 SHA-256 校验。
- 有 ETag/Last-Modified 时执行条件重验证；304 才直接复用。
- 没有 validator 时完整下载并比较 digest；内容变化写入新的内容寻址对象。
- 当前 capture 的 `media_role` / `position` 始终是关系事实来源。

本次设计只收紧“哪些对象允许进入 MinIO”，不放宽上述缓存校验。

## 9. 权限与保留

生产 worker 只获得白名单 prefix 上的 `putObject`、`getObject` 和 `statObject`。worker 不获得:

- `createBucket`。
- 无范围 `listBucket`。
- `deleteObject`。
- denied object class 对应 prefix 的写权限。

清理使用独立身份，并按对象类型的正式 retention policy 执行。建议起点:

| 对象 | 保留原则 |
| --- | --- |
| TikTok / Amazon 业务媒体 | 业务事实存续期，至少覆盖所有有效引用 |
| Amazon normalized capture | 按事实回放与审计周期，当前 365 天 |
| Amazon blocked 截图 | 按受控业务审计周期，当前 180 天 |
| 本地 Runtime/临时文件 | 短周期 best-effort 清理，不配置 MinIO lifecycle |

删除 MinIO 对象前必须确认没有有效 Fact 引用。不能只删对象而留下可被业务读取的悬挂引用。

## 10. 硬切换与实施边界

本 contract revision 采用无旧数据兼容的硬切换:

- 只按当前 contract 校验对象引用；完整且校验通过的引用可读，不因写入时间获得特殊待遇。
- 缺少 `bucket`、`object_key` 或 `content_digest` 的引用一律是无效缓存，必须从业务源重新物化。
- 禁止根据全局 bucket 猜测、修复、回填或迁移不完整引用。
- 不为旧 HTML、network data、Runtime artifact 对象提供读取或重新分类分支。
- 发布时先停止全部旧 worker，再部署并启动同一 contract revision 的 worker；不支持混合版本滚动兼容。
- Runtime DB schema 不变；Fact DB 通过 `20260723_0008` 为 `tk_media_assets` 增加完整持久引用字段。migration 只增加列及空默认值，不回填或迁移旧数据。
- 旧行中的空字段、不完整对象引用和旧 `local_path` 都不构成兼容读取；它们按缓存未命中处理，并从业务源重新物化。
- 回滚时必须先停止所有新 worker，再回退应用与 migration，并只启动同一旧 revision 的 worker；回滚不恢复或改写已存在的 MinIO 对象。

当前实现已收口到本 contract revision：媒体缓存和 Fact 写入只接受完整且远端校验通过的引用；飞书持久附件直接读取 MinIO；通用 Runtime artifact、飞书 raw snapshot 与 FastMoss raw response 保持本地；Amazon 成功只持久化 normalized capture，受阻终态只持久化一张受控截图。
