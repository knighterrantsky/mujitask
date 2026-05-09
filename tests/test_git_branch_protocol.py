from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS = REPO_ROOT / "AGENTS.md"
GIT_WORKFLOW = REPO_ROOT / "docs" / "dev" / "git-workflow.md"
RELEASE_FLOW = REPO_ROOT / "docs" / "ops" / "release-flow.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_agent_indexes_project_git_workflow_before_branch_operations() -> None:
    agents = _read(AGENTS)

    required_tokens = (
        "## Git Branch Protocol",
        "docs/dev/git-workflow.md",
        "docs/ops/release-flow.md",
        "不使用 Codex app 默认 `codex/` 前缀覆盖项目规范",
        "创建或重命名分支后",
        "当前分支查询命令确认实际分支名",
    )
    missing = [token for token in required_tokens if token not in agents]
    assert missing == [], "AGENTS.md is missing branch protocol tokens:\n" + "\n".join(missing)


def test_git_workflow_declares_daily_branch_name_prefixes() -> None:
    workflow = _read(GIT_WORKFLOW)
    agents = _read(AGENTS)
    release = _read(RELEASE_FLOW)

    prefixes = (
        "feature/<topic>",
        "fix/<topic>",
        "docs/<topic>",
        "refactor/<topic>",
        "chore/<topic>",
    )
    for prefix in prefixes:
        assert prefix in workflow
        assert prefix in agents
    assert "分支命名按类型" in release
