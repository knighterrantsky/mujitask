# TikTok → 飞书批量同步协议建议

这份文档描述的是下一步建议新增的批量 task 协议，不代表当前已经全部实现。

## 推荐 task 名称

`tiktok_feishu_batch_sync`

## 适用场景

- OpenClaw 从飞书多维表格批量读取待处理 TikTok 商品链接
- 批量抓取商品信息
- 下载主图到本地并上传成飞书附件
- 回写价格、销量、店铺名称和图片
- 需要知道本次任务哪些成功、哪些失败

## 推荐入参

### 模式 A：表格驱动

```json
{
  "table_url": "https://my.feishu.cn/base/xxx?table=tblxxx&view=vewxxx",
  "access_token_env": "FEISHU_ACCESS_TOKEN",
  "url_field_name": "商品链接",
  "field_mapping": {
    "main_image_file": "商品主图",
    "price_text": "商品价格",
    "sales_count": "销量",
    "shop_name": "店铺名称"
  },
  "batch_size": 20,
  "skip_if_price_exists": true,
  "run_mode": "draft",
  "trace_id": "batch-20260330-001"
}
```

### 模式 B：直接传 URL 列表

```json
{
  "product_urls": [
    "https://www.tiktok.com/shop/pdp/1729440407432826887",
    "https://www.tiktok.com/shop/pdp/1729732615040962895"
  ],
  "run_mode": "draft",
  "trace_id": "batch-20260330-002"
}
```

## 推荐返回结构

```json
{
  "status": "success",
  "summary": {
    "total": 2,
    "success": 1,
    "failed": 1
  },
  "items": [
    {
      "record_id": "recA",
      "product_url": "https://www.tiktok.com/shop/pdp/1729440407432826887",
      "status": "success",
      "data": {
        "price_text": "$17.99",
        "sales_count": 158536,
        "shop_name": "Joyfy-US",
        "main_image_local_path": "runtime/downloads/tiktok_product_images/1729440407432826887-main-image.webp"
      }
    },
    {
      "record_id": "recB",
      "product_url": "https://www.tiktok.com/shop/pdp/1729732615040962895",
      "status": "failed",
      "error": "TikTok page parsing failed"
    }
  ]
}
```

## OpenClaw 最关心的字段

OpenClaw 最终通常只需要这三类信息：

- 本次任务整体是否成功
- 每条记录是否成功
- 失败时的错误信息

所以推荐固定返回：

- `status`
- `summary`
- `items[].status`
- `items[].error`

## 与当前单条 task 的关系

当前已经存在的单条 task：

- `tiktok_product_to_feishu`

它现在只支持单个 `product_url`，代码位置在：

- [tiktok_product_to_feishu.py](/Users/happyzhao/Work/mujitask/src/automation_business_scaffold/tasks/tiktok_product_to_feishu.py#L30)

推荐实现方式不是重写一套抓取逻辑，而是：

1. 批量 task 负责读飞书表格和调度多条记录
2. 每一条记录内部复用现有单条采集逻辑
3. 汇总成功/失败结果后统一返回

## 错误处理建议

批量模式不要因为一条失败就整批报废，推荐：

- 单条失败写入 `items[].error`
- 整批返回 `success` / `partial_success` / `failed`
- 汇总里给出 `total`、`success`、`failed`

这样 OpenClaw 和业务方都更容易看结果。
