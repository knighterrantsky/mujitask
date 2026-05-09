from __future__ import annotations

import ast
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPO_ROOT / "src" / "automation_business_scaffold" / "infrastructure" / "runtime"
RUNTIME_STORE = RUNTIME_ROOT / "runtime_store.py"
BOOTSTRAP = RUNTIME_ROOT / "bootstrap.py"
REPOSITORIES = RUNTIME_ROOT / "repositories"
QUERIES = RUNTIME_ROOT / "queries"

DDL_TOKENS = ("CREATE TABLE", "ALTER TABLE", "DROP TABLE", "CREATE INDEX")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _method_source(path: Path, *, class_name: str, method_name: str) -> str:
    source = _read(path)
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == method_name:
                return "\n".join(lines[item.lineno - 1:item.end_lineno])
    raise AssertionError(f"{class_name}.{method_name} not found")


def _method_call_names(path: Path, *, class_name: str, method_name: str) -> set[str]:
    method_source = _method_source(path, class_name=class_name, method_name=method_name)
    tree = ast.parse(textwrap.dedent(method_source))
    calls: set[str] = set()
    for child in ast.walk(tree):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Name):
            calls.add(child.func.id)
        elif isinstance(child.func, ast.Attribute):
            calls.add(child.func.attr)
    return calls


def test_runtime_store_phase_2_repository_files_exist() -> None:
    expected_repositories = {
        "api_worker_job_repo.py",
        "artifact_object_repo.py",
        "influencer_pool_job_repo.py",
        "notification_outbox_repo.py",
        "resource_lease_repo.py",
        "task_execution_repo.py",
        "task_request_repo.py",
    }
    expected_queries = {
        "db_health_query.py",
        "request_status_query.py",
        "watchdog_query.py",
    }
    assert expected_repositories <= {path.name for path in REPOSITORIES.glob("*.py")}
    assert expected_queries <= {path.name for path in QUERIES.glob("*.py")}


def test_runtime_store_remains_facade_for_extracted_boundaries() -> None:
    runtime_source = _read(RUNTIME_STORE)
    expected_attrs = (
        "_db_health_query",
        "_api_worker_job_repo",
        "_task_execution_repo",
        "_notification_outbox_repo",
        "_artifact_object_repo",
        "_influencer_pool_job_repo",
    )
    for attr in expected_attrs:
        assert attr in runtime_source

    delegated_methods = {
        "collect_db_connection_health": "_db_health_query",
        "enqueue_api_worker_jobs": "_api_worker_job_repo",
        "claim_next_api_worker_job": "_api_worker_job_repo",
        "enqueue_task_executions": "_task_execution_repo",
        "claim_next_browser_execution": "_task_execution_repo",
        "claim_next_outbox": "_notification_outbox_repo",
        "mark_outbox_retry_or_failed": "_notification_outbox_repo",
        "replace_artifacts": "_artifact_object_repo",
        "upsert_influencer_pool_product_jobs": "_influencer_pool_job_repo",
        "claim_influencer_pool_author_job": "_influencer_pool_job_repo",
    }
    for method_name, delegate_attr in delegated_methods.items():
        source = _method_source(RUNTIME_STORE, class_name="RuntimeStore", method_name=method_name)
        assert delegate_attr in source
        assert "self._text(" not in source
        assert "SELECT " not in source
        assert "INSERT " not in source
        assert "UPDATE " not in source


def test_runtime_store_constructor_does_not_bootstrap_or_mutate_schema() -> None:
    init_calls = _method_call_names(RUNTIME_STORE, class_name="RuntimeStore", method_name="__init__")
    forbidden_calls = {"bootstrap_runtime_schema", "ensure_runtime_schema", "bootstrap_schema", "_ensure_schema"}

    assert init_calls.isdisjoint(forbidden_calls)
    assert "bootstrap_runtime_schema(self._engine)" not in _method_source(
        RUNTIME_STORE,
        class_name="RuntimeStore",
        method_name="__init__",
    )
    assert "def bootstrap_schema" in _read(RUNTIME_STORE)


def test_runtime_bootstrap_is_only_explicit_schema_owner_under_runtime_package() -> None:
    bootstrap_source = _read(BOOTSTRAP)
    assert "ensure_runtime_schema" in bootstrap_source

    violations: list[str] = []
    for path in RUNTIME_ROOT.rglob("*.py"):
        if path == BOOTSTRAP:
            continue
        source = _read(path)
        for token in DDL_TOKENS:
            if token in source:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains {token}")
        if "ensure_runtime_schema(" in source:
            violations.append(f"{path.relative_to(REPO_ROOT)} calls ensure_runtime_schema")
    assert violations == []


def test_runtime_claim_paths_fail_fast_before_raw_missing_table_errors() -> None:
    runtime_source = _read(RUNTIME_STORE)
    assert "missing_runtime_schema_message" in runtime_source
    assert "def _ensure_runtime_schema_ready" in runtime_source

    for method_name in (
        "claim_next_task_request",
        "claim_next_api_worker_job",
        "claim_next_browser_execution",
        "claim_browser_execution",
        "claim_next_outbox",
    ):
        source = _method_source(RUNTIME_STORE, class_name="RuntimeStore", method_name=method_name)
        assert "self._ensure_runtime_schema_ready()" in source
