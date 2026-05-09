from __future__ import annotations

from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult

from .pipeline.finalization import run_selection_row_refresh_pipeline


def run_selection_row_refresh_flow(context: HandlerContext) -> HandlerResult:
    return run_selection_row_refresh_pipeline(context)
