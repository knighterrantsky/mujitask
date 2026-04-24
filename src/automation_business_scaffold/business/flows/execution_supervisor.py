from __future__ import annotations

import sys

from automation_business_scaffold.control_plane.supervisor import execution_supervisor as _execution_supervisor

sys.modules[__name__] = _execution_supervisor
