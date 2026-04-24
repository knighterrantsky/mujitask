from __future__ import annotations

import sys

from automation_business_scaffold.domains.competitor_intelligence.flows import (
    tiktok_fastmoss_product_ingest as _flow,
)

sys.modules[__name__] = _flow
