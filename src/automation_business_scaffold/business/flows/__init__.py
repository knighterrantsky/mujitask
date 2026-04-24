from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "ChildRunner": ".child_runner",
    "ChildRunnerConfig": ".child_runner",
    "ChildRunnerEnvelope": ".child_runner",
    "ChildRunnerProgressEvent": ".child_runner",
    "ExecutionProgressEvent": ".execution_supervisor",
    "ExecutionSupervisor": ".execution_supervisor",
    "ExecutionSupervisorCallbacks": ".execution_supervisor",
    "ExecutionSupervisorOutcome": ".execution_supervisor",
    "FORMAL_TASK_CODES": ".runtime_common",
    "INFLUENCER_POOL_TASK_CODE": ".runtime_common",
    "KEYWORD_TASK_CODE": ".runtime_common",
    "PRODUCT_INGEST_TASK_CODE": ".runtime_common",
    "REFRESH_TASK_CODE": ".runtime_common",
    "dispatch_outbox_once": ".runtime_orchestrator",
    "ensure_request_outbox": ".runtime_orchestrator",
    "execute_api_worker_once": ".runtime_orchestrator",
    "execute_browser_once": ".runtime_orchestrator",
    "execute_executor_once": ".runtime_orchestrator",
    "get_task_request_status": ".runtime_orchestrator",
    "run_api_worker_daemon": ".runtime_orchestrator",
    "run_browser_runloop": ".runtime_orchestrator",
    "run_executor_daemon": ".runtime_orchestrator",
    "run_outbox_dispatcher": ".runtime_orchestrator",
    "run_refresh_current_competitor_table_request": ".runtime_orchestrator",
    "run_search_keyword_competitor_products_request": ".runtime_orchestrator",
    "run_sync_tk_influencer_pool_request": ".runtime_orchestrator",
    "run_task_request": ".runtime_orchestrator",
    "run_tiktok_fastmoss_product_ingest_request": ".runtime_orchestrator",
    "run_supervised_handler": ".execution_supervisor",
    "submit_task_request": ".runtime_orchestrator",
    "build_watchdog_store": ".watchdog_scanner",
    "collect_watchdog_candidates": ".watchdog_scanner",
    "decide_watchdog_action": ".watchdog_scanner",
    "execute_watchdog_scan_once": ".watchdog_scanner",
    "run_watchdog_scanner": ".watchdog_scanner",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is not None:
        module = import_module(module_name, __name__)
        value = getattr(module, name)
        globals()[name] = value
        return value
    try:
        value = import_module(f".{name}", __name__)
    except ModuleNotFoundError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    globals()[name] = value
    return value
