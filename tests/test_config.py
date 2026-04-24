from __future__ import annotations

import os
from pathlib import Path

from automation_business_scaffold.config import get_execution_control_defaults
from automation_business_scaffold.project_env import load_project_env_files


def test_get_execution_control_defaults_accepts_legacy_execution_control_env_names(monkeypatch):
    for key in [
        "BUSINESS_EXECUTION_CONTROL_DB_URL",
        "BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT",
        "BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET",
        "BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER",
        "BUSINESS_EXECUTION_CONTROL_ARTIFACT_OBJECT_PREFIX",
        "BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT",
        "BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY",
        "BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY",
        "BUSINESS_EXECUTION_CONTROL_MINIO_REGION",
        "BUSINESS_EXECUTION_CONTROL_MINIO_SECURE",
        "BUSINESS_EXECUTION_CONTROL_MINIO_CREATE_BUCKET",
        "BUSINESS_EXECUTION_CONTROL_SYNC_REFERENCED_FILES",
        "BUSINESS_EXECUTION_CONTROL_REQUESTED_BY",
        "BUSINESS_EXECUTION_CONTROL_WORKER_ID",
    ]:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("EXECUTION_CONTROL_DB_URL", "postgresql+psycopg://demo@/phase1?host=/tmp")
    monkeypatch.setenv("EXECUTION_CONTROL_ARTIFACT_ROOT", "runtime/custom_artifacts")
    monkeypatch.setenv("EXECUTION_CONTROL_ARTIFACT_BUCKET", "custom-bucket")
    monkeypatch.setenv("EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER", "minio")
    monkeypatch.setenv("EXECUTION_CONTROL_ARTIFACT_OBJECT_PREFIX", "phase2/artifacts")
    monkeypatch.setenv("EXECUTION_CONTROL_MINIO_ENDPOINT", "127.0.0.1:9000")
    monkeypatch.setenv("EXECUTION_CONTROL_MINIO_ACCESS_KEY", "minioadmin")
    monkeypatch.setenv("EXECUTION_CONTROL_MINIO_SECRET_KEY", "secret123")
    monkeypatch.setenv("EXECUTION_CONTROL_MINIO_REGION", "cn-test-1")
    monkeypatch.setenv("EXECUTION_CONTROL_MINIO_SECURE", "true")
    monkeypatch.setenv("EXECUTION_CONTROL_MINIO_CREATE_BUCKET", "true")
    monkeypatch.setenv("EXECUTION_CONTROL_SYNC_REFERENCED_FILES", "true")
    monkeypatch.setenv("EXECUTION_CONTROL_REQUESTED_BY", "openclaw-skill")
    monkeypatch.setenv("EXECUTION_CONTROL_WORKER_ID", "worker-legacy")

    defaults = get_execution_control_defaults()

    assert defaults.db_url == "postgresql+psycopg://demo@/phase1?host=/tmp"
    assert defaults.artifact_root == "runtime/custom_artifacts"
    assert defaults.artifact_bucket == "custom-bucket"
    assert defaults.artifact_store_provider == "minio"
    assert defaults.artifact_object_prefix == "phase2/artifacts"
    assert defaults.minio_endpoint == "127.0.0.1:9000"
    assert defaults.minio_access_key == "minioadmin"
    assert defaults.minio_secret_key == "secret123"
    assert defaults.minio_region == "cn-test-1"
    assert defaults.minio_secure is True
    assert defaults.minio_create_bucket is True
    assert defaults.sync_referenced_files is True
    assert defaults.requested_by == "openclaw-skill"
    assert defaults.worker_id == "worker-legacy"


def test_load_project_env_files_uses_executor_then_skill_then_root_precedence(monkeypatch, tmp_path: Path):
    executor_env = tmp_path / "scripts" / "execution_control" / "executor.local.env"
    skill_env = tmp_path / "skills" / "mujitask-tiktok-feishu-sync" / "skill.local.env"
    root_env = tmp_path / ".env"

    executor_env.parent.mkdir(parents=True, exist_ok=True)
    skill_env.parent.mkdir(parents=True, exist_ok=True)

    executor_env.write_text(
        "SHARED_KEY=executor\nEXECUTOR_ONLY=executor\nBUSINESS_EXECUTION_CONTROL_DB_URL=postgresql+psycopg://executor@/runtime?host=/tmp\n",
        encoding="utf-8",
    )
    skill_env.write_text(
        "SHARED_KEY=skill\nSKILL_ONLY=skill\nEXECUTION_CONTROL_DB_URL=postgresql+psycopg://skill@/runtime?host=/tmp\n",
        encoding="utf-8",
    )
    root_env.write_text(
        "SHARED_KEY=root\nROOT_ONLY=root\nBROWSER_PROFILE_REF=root-profile\n",
        encoding="utf-8",
    )

    for key in [
        "SHARED_KEY",
        "EXECUTOR_ONLY",
        "SKILL_ONLY",
        "ROOT_ONLY",
        "BUSINESS_EXECUTION_CONTROL_DB_URL",
        "EXECUTION_CONTROL_DB_URL",
        "BROWSER_PROFILE_REF",
    ]:
        monkeypatch.delenv(key, raising=False)

    loaded = load_project_env_files(root_dir=tmp_path)

    assert loaded == {
        "scripts/execution_control/executor.local.env": [
            "SHARED_KEY",
            "EXECUTOR_ONLY",
            "BUSINESS_EXECUTION_CONTROL_DB_URL",
        ],
        "skills/mujitask-tiktok-feishu-sync/skill.local.env": [
            "SKILL_ONLY",
            "EXECUTION_CONTROL_DB_URL",
        ],
        ".env": [
            "ROOT_ONLY",
            "BROWSER_PROFILE_REF",
        ],
    }
    assert os.environ["SHARED_KEY"] == "executor"
    assert os.environ["EXECUTOR_ONLY"] == "executor"
    assert os.environ["SKILL_ONLY"] == "skill"
    assert os.environ["ROOT_ONLY"] == "root"
    assert os.environ["BUSINESS_EXECUTION_CONTROL_DB_URL"] == "postgresql+psycopg://executor@/runtime?host=/tmp"
    assert os.environ["EXECUTION_CONTROL_DB_URL"] == "postgresql+psycopg://skill@/runtime?host=/tmp"
    assert os.environ["BROWSER_PROFILE_REF"] == "root-profile"


def test_load_project_env_files_does_not_override_existing_process_env(monkeypatch, tmp_path: Path):
    executor_env = tmp_path / "scripts" / "execution_control" / "executor.local.env"
    executor_env.parent.mkdir(parents=True, exist_ok=True)
    executor_env.write_text(
        "BUSINESS_EXECUTION_CONTROL_DB_URL=postgresql+psycopg://file@/runtime?host=/tmp\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_DB_URL", "postgresql+psycopg://process@/runtime?host=/tmp")

    load_project_env_files(root_dir=tmp_path)

    assert os.environ["BUSINESS_EXECUTION_CONTROL_DB_URL"] == "postgresql+psycopg://process@/runtime?host=/tmp"
