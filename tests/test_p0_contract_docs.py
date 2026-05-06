from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_doc(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_handler_contract_docs_freeze_p0_payload_and_projection_examples() -> None:
    doc = _read_doc("docs/arch/handler-contract-design.md")

    required_tokens = (
        "### 5.1.1 P0 冻结样例",
        "### 5.3.1 P0 冻结样例",
        "### 5.4.1 Projection Mapper 输入 / 输出契约",
        "### 6.5.1 `fastmoss_creator_fetch` P0 冻结样例",
        "### 6.7.1 Fact projection P0 冻结样例",
        '"source_table_ref"',
        '"raw_rows"',
        '"source_rows"',
        '"records"',
        "competitor_seed_projection_mapper",
        "competitor_table_projection_mapper",
        "influencer_pool_projection_mapper",
        "competitor_influencer_status_projection_mapper",
        "creator_promotes_product",
        '"projections"',
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "P0 handler contract docs are missing frozen tokens:\n" + "\n".join(
        missing
    )


def test_workflow_docs_include_refresh_keyword_and_influencer_contract_examples() -> None:
    competitor_doc = _read_doc("docs/arch/workflow-competitor-table-design.md")
    influencer_doc = _read_doc("docs/arch/workflow-influencer-pool-sync-design.md")

    competitor_tokens = (
        "## 7. P0 Contract Payload / Result 样例",
        "### 7.1 竞品采集: `feishu_table_read`",
        "### 7.2 竞品采集: Fact projection 到详情写回",
        "### 7.3 关键词搜索竞品写入: `keyword_seed_import`",
        "### 7.4 关键词搜索竞品写入: 种子写入 mapper",
        "competitor_seed_projection_mapper",
        "competitor_table_projection_mapper",
    )
    influencer_tokens = (
        "## 11. P0 Contract Payload / Result 样例",
        "### 11.1 竞品候选读取: `feishu_table_read`",
        "### 11.2 商品达人发现: `product_creator_discovery`",
        "### 11.3 达人同步业务 job: `influencer_creator_sync`",
        "### 11.4 竞品状态批量兜底回写: `competitor_influencer_status_projection_mapper`",
        "influencer_pool_projection_mapper",
        "product_status_writebacks",
    )

    missing_competitor = [token for token in competitor_tokens if token not in competitor_doc]
    missing_influencer = [token for token in influencer_tokens if token not in influencer_doc]

    assert missing_competitor == [], "competitor workflow P0 examples missing:\n" + "\n".join(
        missing_competitor
    )
    assert missing_influencer == [], "influencer workflow P0 examples missing:\n" + "\n".join(
        missing_influencer
    )


