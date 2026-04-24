from __future__ import annotations

from automation_business_scaffold.business.jobs.catalog import TIKTOK_PRODUCT_REQUEST_FETCH_JOB as JOB_DEFINITION

JOB_CODE = JOB_DEFINITION.job_code
HANDLER_CODE = JOB_DEFINITION.handler_code

__all__ = ["HANDLER_CODE", "JOB_CODE", "JOB_DEFINITION"]
