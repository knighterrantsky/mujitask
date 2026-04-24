from __future__ import annotations

import sys

from automation_business_scaffold.domains.competitor_intelligence.flows import (
    refresh_current_competitor_table as _flow,
)

sys.modules[__name__] = _flow
