# Amazon 竞品表商品详情采集需求

日期: 2026-07-14

更新: 2026-07-23

状态: 已批准，实施中

## 1. 业务目标

单商品正式任务 `refresh_amazon_product_row_by_asin` 以飞书 `Amazon竞品表` 一条来源记录为入口，读取该行 ASIN，通过项目配置的 Chrome CDP 或指纹浏览器访问 Amazon 美国站商品页，沉淀采集证据与 Amazon 独立事实，并将结果写回同一飞书 record_id。

批量正式任务 `refresh_current_amazon_product_table` 扫描同一张 `Amazon竞品表`，只选择 `采集标签` 文本值严格等于 `T` 的记录，并在同一个顶层 `task_request` 下为每条有效记录创建一个幂等的 `amazon_product_row_refresh` 行级主 Job。批量入口不创建单商品子 Request，不复制浏览器解析、事实入库、媒体或飞书投影逻辑。关键词搜索仍不在当前实现范围。

### 1.1 Amazon 业务入口隔离

- 本文中的 Amazon 业务不得通过 TikTok 对话或 `mujitask-tiktok-feishu-sync` 接收指令。
- 当前及后续所有 Amazon workflow 统一由独立 Skill `mujitask-amazon-feishu-sync` 暴露，并部署到独立 OpenClaw agent `amazon-ops` 的 `workspace-amazon`。
- Amazon 使用部署配置 `MUJITASK_AMAZON_FEISHU_ACCOUNT_ID` 指向客户本地实际存在的飞书机器人账号，不固定账号别名。Amazon 必须使用独立飞书群聊，并由精确 `peer.kind=group` / `peer.id=oc_*` 绑定路由到 `amazon-ops`；不得落入 TikTok 的账号级兜底路由。
- Amazon workspace 只安装 Amazon Skill，不安装 TikTok Skill；TikTok workspace 同样不得安装 Amazon Skill。
- 飞书对话、Skill 和 OpenClaw workspace 独立不等于复制后台运行系统。Runtime DB、executor、worker、watchdog、Fact DB 实例和对象存储 bucket 继续按既有架构共享，并通过 Amazon 独立事实表和对象前缀隔离。
- 飞书机器人 App ID、App Secret 和访问令牌属于部署密钥，只能保存在 OpenClaw/部署 secret 配置中，不能进入需求文档、机器契约、Skill 生成产物、任务 payload、日志或通知。

## 2. 首期范围

- 站点仅支持美国站 `amazon.com`，marketplace code 固定为 `US`。
- 商品身份使用 ASIN，不使用 Seller SKU。
- ASIN 先去除首尾空格并转大写，再按 `^[A-Z0-9]{10}$` 校验。
- 系统只完整采集来源行对应的当前 ASIN；保存 Parent ASIN、页面暴露的 Child ASIN 和变体属性，但不逐个访问其他 Child ASIN。
- 采集内容包括标题、品牌、类目、卖点、描述、主图/侧边栏图片、价格、评分、评论数、过去一个月购买人数页面展示值、库存状态、Parent/Child ASIN、变体属性、卖家、配送方式、送达日期、包装规格、Buy Box、优惠券、促销、BSR 排名和技术参数。
- 当前飞书最终写回白名单固定为 `主图`、`侧边栏图片`、`30天购买人数`、`送达日期`、`包装规格`、`促销活动记录` 六个字段；其余采集结果继续进入 Amazon Fact DB，但不得发送到飞书字段写入接口。
- 不采集评论明细、问答明细、A+ Content 或全部第三方 Offer。

### 2.4 30天购买人数口径

- `30天购买人数` 只取 Amazon 商品页受控节点 `#social-proofing-faceout-title-tk_bought` 中符合 `<展示值> bought in past month` 的可见英文文案。
- 飞书只写文案中的展示值。例如页面显示 `500+ bought in past month` 时，写入文本 `500+`。
- `500+` 是 Amazon 页面给出的区间展示值，系统不得转换为精确数值 `500`，不得去掉 `+`，也不得与订单量、销量或评论数混用。
- 页面没有该节点、节点不可见或文案不匹配时，证据状态为 `missing`，不写该字段并保留飞书原值；该可选指标缺失不应单独把整行降级为 `partial_success`。

### 2.3 侧边栏图片口径

- `侧边栏图片` 的顺序对应 Amazon 商品页左侧缩略图栏 `#altImages` 暴露的有序图库，但附件内容必须使用每个图库项对应的高清原图，内部标准字段仍为 `media.gallery_images`；不得把缩略图栏当前渲染的低分辨率 `img.src` 直接作为最终媒体。
- Browser 必须先从当前商品图片块的 `ImageBlockATF.colorImages.initial` 读取图库项，按该数组顺序绑定侧边栏项与同一项的高清候选；候选优先级为 `hiRes`、`data-old-hires`、最大尺寸动态图片、`large`，视频项必须排除。
- `thumb` / `#altImages img.src` 只用于识别侧边栏顺序，不能用于推断高清资源。缩略图与 `hiRes` 可能使用不同 Amazon 资产 ID，例如同一图库项的缩略图为 `51…`，高清图为 `71…`；因此禁止仅移除缩略图 URL 的 `._AC_..._`、`._US..._` 等变换段后当作高清原图。
- 只有已由同一图库项绑定得到的高清候选才执行 Amazon CDN URL 规范化：移除 `._AC_..._`、`._SX..._`、`._SY..._`、`._SR..._`、`._SL..._`、`._UF..._`、`._QL..._` 等尺寸、裁剪、质量或格式变换段，并下载对应原始资源。
- 图片必须使用解析后的高清原图 URL 先下载为可验证的图片文件，再写入对象存储并上传为飞书附件；不得将 Amazon 远程 URL 直接写入附件字段，也不得下载后上传仍可识别为缩略图派生 URL 的文件。
- 无法解析或下载高清原图时，该图片按媒体缺失处理并使本次采集进入 `partial_success`；不得回退上传缩略图充当成功结果。
- 同一条商品记录按页面顺序保留全部可规范化的 Amazon 图片 URL，并去除重复 URL。
- 重复采集时，规范化 Amazon CDN URL 只能用于定位历史媒体候选，不能单独证明图片内容未变化。缓存候选必须仍属于当前环境的 Amazon 受控对象前缀、MinIO 对象字节与已记录 `size_bytes/content_digest` 一致，并通过 `ETag/Last-Modified` 条件重验证后才能复用；CDN 返回新内容、验证器缺失、对象缺失或校验失败时必须重新下载并按新内容 digest 物化。对象复用不继承历史关系坐标，本次采集的 `media_role/position` 保持不变。
- 旧媒体记录没有 HTTP 验证器时，首次再次观察必须完整下载并比较 SHA-256，不能仅因 URL 未变化而直接复用。图片下载并发不属于本次缓存优化范围，仍保持现有串行下载语义。
- 本次明确观察到有效侧边栏图片时，飞书 `侧边栏图片` 使用附件数组覆盖旧值；字段缺失或采集失败时保留旧值。

### 2.2 包装规格与送达日期口径

- `包装规格` 只取 Product information → Item details 中 `Number of Items` 的可见值，不使用 `Unit Count`、变体标题或商品数量选择器替代；页面没有该字段或值为空时写固定文本 `没有包装规格`。
- `送达日期` 只取当前 Featured Offer / Buy Box 主配送消息中以 `FREE delivery` 开头的可见文案。
- Fact DB 保留已移除地址、邮编、`Or fastest delivery`、倒计时和账户文本的英文主配送文案；飞书 `送达日期` 再移除 `FREE delivery` 标签、订单门槛和英文星期，并将日期或日期范围转换为中文展示。
- 单日格式为 `M月D号`，同月日期范围格式为 `M月D-D号`，跨月日期范围格式为 `M月D号-M月D号`。例如 `FREE delivery Saturday, July 25` 写为 `7月25号`，`FREE delivery July 25 - 26` 写为 `7月25-26号`，`FREE delivery July 29 - August 2` 写为 `7月29号-8月2号`。
- 页面未观察到合格的 `FREE delivery` 文案，或净化后的文案无法提取日期时，不写 `送达日期`，保留飞书原值。

### 2.1 促销活动口径

- 促销是当前 Child ASIN、浏览器 profile、配送地区和采集时间共同约束的 Offer 快照，不是商品静态属性。
- 首期促销活动采用白名单，只认 `coupon` 和 `limited_time_deal`。同一 ASIN 同时出现多条白名单活动时逐条保留，不相互覆盖。
- `Save ... at checkout`、Prime Member Price、Exclusive Prime Price、Prime Day Deal、Subscribe & Save、数量折扣、条件购买折扣、普通降价、List Price、Typical Price、Regular Price 及无法明确归类的其他文案均不属于本业务的促销活动，不进入 `promotions[]` 或飞书 `促销活动记录`。
- `coupon` 必须来自页面明确的 `Coupon` / `Apply ... coupon` / `Save ... with coupon` 可见文案，并保存百分比或固定金额折扣。飞书折后价以同一 Featured Offer 当前价格为基数计算：百分比 Coupon 使用 `price * (1 - discount / 100)`，固定金额 Coupon 使用 `price - discount`，结果按美元四舍五入保留两位小数且不得小于 0。
- `limited_time_deal` 必须存在明确的 `Limited time deal` 英文活动标志；只保存活动标志和该报价区的页面活动价，不保存页面折扣百分比或 List/Typical/Regular Price 等对比价。
- 每条活动的采集时间统一使用父 capture 的 `captured_at`。飞书展示时转换为北京时间 `M-D HH:mm`，不显示年份和秒。
- 飞书 `促销活动记录` 每次写入当前采集快照并覆盖旧值，不做历史追加。每条活动使用连续两行展示：第一行是 `coupon | 折扣 | 折后价` 或 `Limited time deal | 活动价`，第二行是采集时间；多条活动连续写入各自的两行区块，不插入空行。本次明确观察到无白名单促销时写入第一行 `当前没有促销活动`、第二行采集时间，并覆盖旧促销记录。例如 Coupon 写为 `coupon | 10% | $35.99\n7-20 07:08`。
- 促销原始文案只能来自当前报价区的可见语义文本；`script`、`style`、隐藏兑换参数、token、Cookie 或账户/地址文本不得进入 capture、Fact DB、飞书、日志或通知。
- `coupon_text` 作为旧投影的精简兼容字段保留；完整促销事实以结构化 `promotions[]` 为准，其时间统一绑定父 capture 的 `captured_at`。

## 3. 输入与身份规则

单商品正式任务业务输入仅包含：

- `table_ref`：指向配置别名 `AMAZON_PRODUCTS` 的飞书表引用。
- `source_record_id`：本次读取和写回的飞书来源行。

批量正式任务业务输入仅包含 `table_ref=AMAZON_PRODUCTS`。筛选字段和值固定为 `采集标签=T`，不允许用户从对话中改成其他字段或其他值；字段缺失、空值或不是严格大写 `T` 的记录一律不进入采集。

Amazon Skill 从自身 `skill.local.env` 读取 Base URL、Table ID、View ID，提交时必须把拼接后的无密钥表 URL 作为 `table_refs.AMAZON_PRODUCTS` 配置快照写入任务 payload。该快照不是用户业务输入；飞书 access token 不进入 payload。worker 只消费任务快照，缺失时 fail closed，不得从项目 `.env`、`executor.local.env` 或进程环境解析 `AMAZON_PRODUCTS` 表路由。

浏览器 profile、Runtime DB、Fact DB、对象存储地址及密钥不得进入正式任务 payload，由项目运行配置解析。系统根据规范化 ASIN 构造 `https://www.amazon.com/dp/{asin}`，不信任飞书链接中的跟踪参数。

请求 ASIN 与页面解析 ASIN 不一致时不得把页面商品字段写入来源行。页面明确不可售、下架或不存在时，仍需保存终态事实并写回 `unavailable`。

## 4. 业务流程

### 4.1 单商品流程

1. 读取 `source_record_id` 对应飞书行并校验 ASIN。
2. 以项目配置的浏览器 profile 访问美国站 canonical URL。
3. 按页面内嵌数据、同源页面响应、稳定语义 DOM、受控文本区块的顺序解析字段，并保存字段来源与完整度。
4. 将 `normalized_capture` 写入 MinIO；主图/图库在媒体同步阶段写入 MinIO。HTML、page-data、network data、日志和普通截图只作为本地临时/诊断文件，不进入 MinIO。只有 blocked/captcha/access-blocked 的受控终态截图可以作为长期业务审计证据持久化。
5. 将商品、快照、Offer、变体、BSR、媒体和原始 capture 索引写入 Amazon 独立事实表。
6. 只从六字段写回白名单中投影本次明确观察到的字段；`missing` 字段保留飞书旧值。
7. `采集状态`、`上次采集时间`、`字段完整度`、脱敏错误摘要及其他非白名单字段一律不写入飞书，也不得阻断浏览器采集和事实持久化。

### 4.2 批量流程

1. 读取 `AMAZON_PRODUCTS` 对应的 Amazon竞品表。
2. 只保留 `采集标签` 严格等于 `T` 且 ASIN 合法的记录；其他记录不创建行级 Job。
3. 按 `source_record_id` 在当前批量 Request 下为每条候选记录创建一个幂等的 `amazon_product_row_refresh` 行级主 Job。
4. 行级主 Job 复用单商品采集使用的 Amazon 浏览器采集和行持久化能力；浏览器仍使用独立 `task_execution`，完成后恢复同一个行级主 Job。
5. 批量 Request 等待并汇总行级 Job 的最终业务状态，飞书对话只收到一条最终通知。

## 5. 状态口径

- `pending`、`collecting`、`persisting` 为非终态。
- `success`、`partial_success`、`unavailable`、`blocked`、`failed` 为终态。
- `blocked` 表示验证码、机器人页或访问限制；必须只保存一张受控终态证据截图，不生成 normalized capture 或商品媒体，不允许自动绕过，并在 Runtime 层按失败结果收敛。
- `partial_success` 表示身份与事实已完成，但部分可选字段、媒体或飞书投影缺失。
- `unavailable` 是已成功持久化的商品终态事实，不等同于系统执行失败。

## 6. 数据与存储边界

- Amazon 使用同一 Fact DB 实例中的 `amazon_*` 独立表，不写入 `tk_*` 表，也不建立跨平台外键。
- MinIO 只保存长期业务对象，采用默认拒绝和显式白名单；Amazon 准入对象仅为商品媒体、`normalized_capture` 与 blocked/captcha/access-blocked 受控截图。
- 对象存储复用现有 bucket，通过 Amazon 专用 prefix 隔离；首期不新建 bucket，生产 worker 不创建 bucket。
- 持久引用至少包含 `bucket + object_key + content_digest`；`local_path` 只用于当前进程临时文件，不能进入 Amazon Fact 或作为 Browser/API 跨进程定位。
- 成功或 partial success 不要求 HTML、page-data、network data 或成功截图。上述文件不得因体积、排障或 Runtime 紧凑引用要求被提升到 MinIO。
- Runtime DB schema 不因本需求变化，只复用现有 task、execution、job、lease、artifact 和 outbox 能力。
- 生产 daemon/worker 不执行 DDL；表和索引只由 migration user 通过 migration 创建。

## 7. 验收口径

1. 合法飞书 ASIN 能触发四阶段单行 workflow，并将结果写回同一来源记录。
2. 同一来源行和 ASIN 重试不会产生重复商品主档、重复快照、重复变体关系或重复媒体关系。
3. 浏览器结果只在 Runtime DB 中保存身份、状态、完整度、完整的 `normalized_capture`/受控 blocked 证据引用，以及已去除 query/fragment 且绑定 Amazon/US/ASIN 的紧凑媒体来源引用；不内联完整 HTML、标准化 capture 或媒体正文，也不返回成功 HTML/page-data/network-data 的持久引用。
4. `missing` 字段不清空飞书旧值；只有 `observed` 或 `explicitly_unavailable` 字段可写回。
5. 非美国站、非法 ASIN、身份不一致、blocked、Fact DB 失败、白名单业务对象存储失败和飞书写回失败均按受控错误口径收敛；本地诊断文件未进入 MinIO不是对象存储失败。
6. 现有 TikTok / FastMoss workflow、`tk_*` 事实表和 browser fallback 语义不受影响。
7. 动态 Coupon ID 和 Limited Time Deal 能生成结构化促销；同页同时存在 Coupon 与结账折扣时只保留 Coupon。
8. 无白名单促销页面返回空数组；结账折扣、Prime 会员价、Prime Day Deal、Subscribe & Save、数量/条件购买折扣、普通划线价、促销解释文本、Prime 配送宣传和页面导航不得误判为促销。
9. 结构化促销不得含有隐藏脚本、样式、兑换 URL/参数、token、Cookie 或其他敏感内容。
10. revision 1 的历史文本促销仍可由持久化边界读取；revision 2 及后续 capture 只产生结构化促销对象。revision 4 新 capture 必须按 `colorImages.initial` 绑定高清图库；revision 3 仍可读取，但只有重新采集后才具备高清资产 ID 保证。revision 5 新 capture 新增可选 `commerce.bought_past_month` 文本及对应字段证据，revision 1–4 继续兼容读取且无需重写。
11. Coupon 写回第一行包含英文类型、折扣和以当前 Featured Offer 价格计算的两位小数折后价，第二行包含北京时间 `M-D HH:mm`；Limited Time Deal 第一行只包含英文类型和页面活动价，第二行使用相同时间格式。
12. `促销活动记录` 使用覆盖写入；本次明确观察到空数组时写入 `当前没有促销活动\nM-D HH:mm`，覆盖旧值且不追加历史记录；只有证据状态为 `missing` 时保留旧值。
13. Product information → Item details 中 `Number of Items=1` 时写回 `包装规格=1`；字段缺失时写回 `包装规格=没有包装规格`。
14. Buy Box 同时出现 `FREE delivery August 6 - 19 to Los Angeles 90001` 和 `Or fastest delivery August 6 - 17` 时，capture 保留 `FREE delivery August 6 - 19`，飞书 `送达日期` 只写 `8月6-19号`；两者均不包含地址或次级配送文案。
15. Amazon `#altImages` 中明确观察到的有效图库项，必须按 `ImageBlockATF.colorImages.initial` 的同项 `hiRes` 映射和页面顺序上传到同一条飞书记录的 `侧边栏图片` 附件字段；缩略图资产 ID 与高清资产 ID 不同时必须使用高清资产，并覆盖该字段旧附件。
16. 任意 Amazon 单行或批量终态写入发送给飞书的字段集合必须是 `主图`、`侧边栏图片`、`30天购买人数`、`送达日期`、`包装规格`、`促销活动记录` 的子集；状态、错误、标题、品牌、价格及其他商品字段不得写入。
17. Amazon 指令只能由 `amazon-ops` workspace 中的 `mujitask-amazon-feishu-sync` 接收；受理回执和最终通知的 `reply_target.accountId` 必须等于部署配置 `MUJITASK_AMAZON_FEISHU_ACCOUNT_ID`，不得在代码中固定本地账号别名，也不得使用 TikTok workspace。
18. 批量任务只为 `采集标签=T` 的记录创建行级主 Job；`t`、空值、其他标签和字段缺失均不采集。
19. 同一批量请求内，每个 `source_record_id` 最多创建一个 `amazon_product_row_refresh` Job；所有 Job 与父任务使用同一个 `request_id`，父任务只发送一次汇总通知。
20. 生产 daemon 不指定 `request_id` 时，也必须能够领取批量 Request 下的行级 Job 并完成浏览器执行、恢复、持久化和汇总；禁止通过测试专用定向 claim 绕过该验收。
21. 页面显示 `500+ bought in past month` 时，飞书同一来源行必须写入 `30天购买人数=500+`；不得写为数值 `500` 或完整英文句子。页面未观察到合格文案时保留飞书原值，且不因该可选字段缺失单独降低采集状态。

架构与机器契约以 [Amazon 商品详情采集 Workflow 与事实存储设计](../../arch/workflow-amazon-product-detail-design.md) 及 `contracts/**` 为准；在相关 completion gate 通过前，不得声明首期能力完成。
