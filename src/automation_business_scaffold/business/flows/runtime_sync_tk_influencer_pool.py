from __future__ import annotations

import sys

from automation_business_scaffold.domains.competitor_intelligence.flows import (
    sync_tk_influencer_pool as _flow,
)

sys.modules[__name__] = _flow
