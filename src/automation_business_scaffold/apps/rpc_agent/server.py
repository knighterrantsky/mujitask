from __future__ import annotations

import os

import uvicorn
from automation_framework.agent.server import create_app

from automation_business_scaffold.registry import build_task_registry

app = create_app(build_task_registry())


def main() -> None:
    host = os.getenv("AGENT_HOST", "127.0.0.1")
    port = int(os.getenv("AGENT_PORT", "8110"))
    uvicorn.run(
        "automation_business_scaffold.apps.rpc_agent.server:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
