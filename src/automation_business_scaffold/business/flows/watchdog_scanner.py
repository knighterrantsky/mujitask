from __future__ import annotations

import sys

from automation_business_scaffold.control_plane.watchdog import scanner as _scanner

sys.modules[__name__] = _scanner
