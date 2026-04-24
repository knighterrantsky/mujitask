from __future__ import annotations

import sys
from importlib import import_module

_main = import_module("automation_business_scaffold.apps.daemons.browser_worker.main")

sys.modules[__name__] = _main
