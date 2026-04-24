"""RPC agent service entrypoint."""

from automation_business_scaffold.apps.rpc_agent.server import app, main

__all__ = ["app", "main"]
