from __future__ import annotations

from automation_business_scaffold.config import get_execution_control_defaults


def test_get_execution_control_defaults_accepts_legacy_execution_control_env_names(monkeypatch):
    for key in [
        "BUSINESS_EXECUTION_CONTROL_DB_URL",
        "BUSINESS_EXECUTION_CONTROL_DB_PATH",
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
