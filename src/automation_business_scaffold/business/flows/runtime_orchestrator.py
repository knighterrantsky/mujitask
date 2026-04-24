from __future__ import annotations

import sys

from automation_business_scaffold.control_plane.executor import runner as _runner

sys.modules[__name__] = _runner
