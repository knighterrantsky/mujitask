from __future__ import annotations


RUNTIME_SCHEMA_OWNER = "infrastructure.runtime.bootstrap"


def missing_runtime_schema_message() -> str:
    return (
        "Runtime DB schema is not available. Run the explicit runtime schema "
        "bootstrap or migration path before starting executor, workers, watchdog, "
        "or outbox dispatcher."
    )
