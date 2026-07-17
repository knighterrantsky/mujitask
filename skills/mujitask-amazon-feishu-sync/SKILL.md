---
name: "mujitask-amazon-feishu-sync"
description: >-
  Submits confirmed single-row or T-tagged batch collection tasks for Amazon竞品表. Use when
  the user identifies one Feishu source record or asks to collect table rows whose 采集标签 is
  T, and explicitly confirms submission.
metadata:
  short-description: "Amazon竞品表任务提交"
---

# Amazon竞品表采集

<!-- GENERATED FROM skill.spec.yaml. DO NOT EDIT SKILL.md BY HAND. -->

## Scope

- Submit confirmed Amazon竞品表 single-row or 采集标签=T batch tasks.
- Task: `refresh_amazon_product_row_by_asin`
- Target: `Amazon竞品表`
- Task: `refresh_current_amazon_product_table`
- Target: `Amazon竞品表`

## Trigger

- The user asks to collect or refresh one identified Amazon竞品表 record.
- The user asks to collect Amazon竞品表 rows whose 采集标签 is T.
- The user asks to batch-collect or refresh the Amazon竞品表; always apply the fixed 采集标签=T selector.

Do not trigger for:

- Conceptual, deployment, TikTok, or keyword-search requests.
- ASIN-only single-row requests without a Feishu source_record_id.

## Input

- `confirmation`: Explicit confirmation of the latest preview.
  - Require confirmation before submission.
- `source_record_id`: Feishu record id in Amazon竞品表.
  - Require an explicit record id; never substitute ASIN or URL.

## Submit

1. Select the single-row or fixed T-tagged batch intent and show a one-line preview.
2. Submit only after explicit confirmation.
3. Run the command once and return its request_id.

`amazon_product_row_refresh`:

```bash
bash skills/mujitask-amazon-feishu-sync/run_task.sh --intent "amazon_product_row_refresh" --source-record-id "<source_record_id>"
```

`amazon_product_table_refresh`:

```bash
bash skills/mujitask-amazon-feishu-sync/run_task.sh --intent "amazon_product_table_refresh"
```

## Output

Success:

```text
request_id: <request_id>
```

Failure:

```text
任务提交失败：<short safe reason>
```

## Guardrails

- Require explicit confirmation.
- Never accept ASIN or URL as source_record_id.
- Never change the batch selector from 采集标签=T.
- Require the bound amazon-ops Feishu group context.
- Do not expose credentials or internal configuration.
