from __future__ import annotations

import sys

from automation_business_scaffold.domains.competitor_intelligence.flows import (
    search_keyword_competitor_products as _flow,
)

sys.modules[__name__] = _flow
