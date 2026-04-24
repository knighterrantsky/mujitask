from __future__ import annotations

import sys

from automation_business_scaffold.control_plane.supervisor import child_runner as _child_runner

sys.modules[__name__] = _child_runner
