from __future__ import annotations

import sys

from automation_business_scaffold.capabilities.channels.outbox import implementations as _implementations

sys.modules[__name__] = _implementations
