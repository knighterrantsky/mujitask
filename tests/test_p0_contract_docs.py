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
        "### 7.1 竞品表刷新: `feishu_table_read`",
        "### 7.2 竞品表刷新: Fact projection 到详情写回",
        "### 7.3 关键词竞品入库: `fastmoss_product_search`",
        "### 7.4 关键词竞品入库: 种子行写入",
        "competitor_seed_projection_mapper",
        "competitor_table_projection_mapper",
    )
    influencer_tokens = (
        "## 11. P0 Contract Payload / Result 样例",
        "### 11.1 竞品候选读取: `feishu_table_read`",
        "### 11.2 商品达人发现: `fastmoss_product_fetch`",
        "### 11.3 达人详情采集: `fastmoss_creator_fetch`",
        "### 11.4 达人池写入: `influencer_pool_projection_mapper` -> `feishu_table_write`",
        "### 11.5 竞品状态回写: `competitor_influencer_status_projection_mapper`",
    )

    missing_competitor = [token for token in competitor_tokens if token not in competitor_doc]
    missing_influencer = [token for token in influencer_tokens if token not in influencer_doc]

    assert missing_competitor == [], "competitor workflow P0 examples missing:\n" + "\n".join(
        missing_competitor
    )
    assert missing_influencer == [], "influencer workflow P0 examples missing:\n" + "\n".join(
        missing_influencer
    )


def test_achieve_comparator_contract_is_documented_as_read_only_acceptance_boundary() -> None:
    doc = _read_doc("docs/arch/rewrite-acceptance-contract.md")

    required_tokens = (
        "## 8. Achieve Comparator P0 契约",
        "### 8.1 Comparator Payload",
        "### 8.2 Comparator Result",
        "### 8.3 三条 Workflow 对比范围",
        '"compare_scope"',
        '"normalization"',
        '"allowed_differences"',
        "refresh_current_competitor_table",
        "search_keyword_competitor_products",
        "sync_tk_influencer_pool",
        "禁止被 runtime 主路径 import",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "achieve comparator contract docs are missing tokens:\n" + "\n".join(
        missing
    )


def test_rewrite_plan_links_p0_contract_freeze_outputs() -> None:
    doc = _read_doc("docs/dev/rewrite-development-plan.md")

    required_tokens = (
        "P0 契约冻结产物",
        "`feishu_table_read` / `feishu_table_write` payload/result",
        "`fastmoss_product_search` payload/result",
        "`fastmoss_creator_fetch` payload/result",
        "Fact projection",
        "`achieve` comparator",
        "P0 明确不交付",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "rewrite plan P0 contract freeze links are missing:\n" + "\n".join(
        missing
    )
