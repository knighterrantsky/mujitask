# TikTok → 飞书批量 URL 同步协议

这份文档描述当前已经实现的 `tiktok_feishu_batch_sync` 正式协议。

## Task 名称

`tiktok_feishu_batch_sync`

## 适用场景

- OpenClaw 一次拿到 10 到 20 个 TikTok 商品链接
- 需要顺序处理这些 URLs
- 每条 URL 都走完整的“抓取 -> 下载图片 -> 上传飞书附件 -> 新建飞书记录”链路
- 需要随机 delay，避免中间外部请求过于密集
- 需要知道哪些插入成功、哪些被去重跳过、哪些失败

## 输入协议

```json
{
  "product_urls": [
    "https://www.tiktok.com/shop/pdp/1729440407432826887",
    "https://www.tiktok.com/shop/pdp/1729732615040962895"
  ],
  "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
  "access_token_env": "FEISHU_ACCESS_TOKEN",
  "run_mode": "approval_required",
  "trace_id": "batch-20260331-001"
}
```

## 必填参数

- `product_urls`
- `table_url`
- `access_token_env`

## 可选参数

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

## 默认节流参数

- `step_delay_sec = 1.0`
- `step_delay_jitter_sec = 1.0`
- `record_delay_sec = 2.0`
- `record_delay_jitter_sec = 2.0`
- `pause_every = 5`
- `pause_sec = 8.0`
- `continue_on_error = true`

## 去重规则

每条 URL 都按下面顺序执行：

1. 先按 `产品链接` 查整张目标飞书表
2. 如果没命中，再抓取商品并按 `SKU-ID` 查整张目标飞书表
3. 命中任一条件就返回 `skipped_existing`
4. 不更新原记录，也不重复新建

## 返回结构

```json
{
  "summary": {
    "total": 2,
    "processed": 2,
    "inserted": 1,
    "skipped_existing": 1,
    "previewed": 0,
    "failed": 0
  },
  "items": [
    {
      "status": "inserted",
      "record_id": "recA",
      "product_url": "https://www.tiktok.com/shop/pdp/1729440407432826887",
      "product_id": "1729440407432826887",
      "fields": {
        "产品链接": "https://www.tiktok.com/shop/pdp/1729440407432826887",
        "SKU-ID": "1729440407432826887",
        "图片": [
          {
            "file_token": "boxcn..."
          }
        ],
        "标题": "Sample Title",
        "节日": "情人节",
        "价格": "17.99"
      }
    },
    {
      "status": "skipped_existing",
      "record_id": "recB",
      "product_url": "https://www.tiktok.com/shop/pdp/1729732615040962895",
      "product_id": "1729732615040962895",
      "fields": {},
      "duplicate_reason": "sku",
      "existing_record_id": "recB"
    }
  ],
  "settings": {
    "run_mode": "approval_required",
    "write_back": true,
    "step_delay_sec": 1.0,
    "step_delay_jitter_sec": 1.0,
    "record_delay_sec": 2.0,
    "record_delay_jitter_sec": 2.0,
    "pause_every": 5,
    "pause_sec": 8.0,
    "continue_on_error": true
  }
}
```

## OpenClaw 最关心的字段

- `summary.inserted`
- `summary.skipped_existing`
- `summary.failed`
- `items[].status`
- `items[].error`

## 与单条 task 的关系

单条正式 task：

- `tiktok_feishu_single_sync`

底层字段构建 task：

- `tiktok_product_to_feishu`

实现关系：

1. 批量 task 不从飞书读取待处理行
2. 批量 task 直接消费 `product_urls[]`
3. 批量 task 内部顺序复用单条插入链路
4. 每条记录之间继续加随机 delay
