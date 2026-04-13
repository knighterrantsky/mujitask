from __future__ import annotations

from automation_business_scaffold.config import get_execution_control_defaults


def test_get_execution_control_defaults_accepts_legacy_execution_control_env_names(monkeypatch):
    for key in [
        "BUSINESS_EXECUTION_CONTROL_DB_URL",
        "BUSINESS_EXECUTION_CONTROL_DB_PATH",
        "BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT",
        "BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET",
        "BUSINESS_EXECUTION_CONTROL_REQUESTED_BY",
        "BUSINESS_EXECUTION_CONTROL_WORKER_ID",
    ]:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("EXECUTION_CONTROL_DB_URL", "postgresql+psycopg://demo@/phase1?host=/tmp")
    monkeypatch.setenv("EXECUTION_CONTROL_ARTIFACT_ROOT", "runtime/custom_artifacts")
    monkeypatch.setenv("EXECUTION_CONTROL_ARTIFACT_BUCKET", "custom-bucket")
    monkeypatch.setenv("EXECUTION_CONTROL_REQUESTED_BY", "openclaw-skill")
    monkeypatch.setenv("EXECUTION_CONTROL_WORKER_ID", "worker-legacy")

    defaults = get_execution_control_defaults()

    assert defaults.db_url == "postgresql+psycopg://demo@/phase1?host=/tmp"
    assert defaults.artifact_root == "runtime/custom_artifacts"
    assert defaults.artifact_bucket == "custom-bucket"
    assert defaults.requested_by == "openclaw-skill"
    assert defaults.worker_id == "worker-legacy"
