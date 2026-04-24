from __future__ import annotations

import sys

from automation_business_scaffold.capabilities._implementations import api as _api

sys.modules[__name__] = _api
