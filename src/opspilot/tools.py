"""LangChain tools built from the OpsPilot MCP servers (stdio, via langchain-mcp-adapters).

By default the agent gets the read-only ``k8s`` and ``kb`` servers. The gated
``actions`` server is only added when explicitly requested, and even then it cannot
change anything without a human-approved token.
"""

import os
import sys
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StdioConnection

from opspilot.mcp_servers.common.audit import new_run_id

Mode = Literal["live", "replay"]
ROOT = Path(__file__).resolve().parents[2]


def _connection(module: str, args: list[str], env: dict[str, str]) -> StdioConnection:
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-m", module, *args],
        "env": env,
        "cwd": str(ROOT),
    }


def server_connections(
    *,
    mode: Mode = "live",
    fixture: Path | None = None,
    include_actions: bool = False,
    run_id: str | None = None,
) -> dict[str, StdioConnection]:
    """stdio connection specs for each server; all share one run id in the audit log."""
    if mode == "replay" and fixture is None:
        raise ValueError("replay mode needs a fixture path")
    env = {**os.environ, "OPSPILOT_RUN_ID": run_id or new_run_id()}
    k8s_args = ["--mode", mode] + (["--fixture", str(fixture)] if fixture else [])
    connections: dict[str, StdioConnection] = {
        "k8s": _connection("opspilot.mcp_servers.k8s_readonly", k8s_args, env),
        "kb": _connection("opspilot.mcp_servers.knowledge", [], env),
    }
    if include_actions:
        if mode == "replay":
            raise ValueError("the actions server is live-only; it is never used in replay")
        connections["actions"] = _connection("opspilot.mcp_servers.actions", [], env)
    return connections


def mcp_client(**kwargs: object) -> MultiServerMCPClient:
    """A client over the requested servers (see ``server_connections``)."""
    return MultiServerMCPClient(server_connections(**kwargs))  # type: ignore[arg-type]


async def load_tools(
    *,
    mode: Mode = "live",
    fixture: Path | None = None,
    include_actions: bool = False,
    run_id: str | None = None,
) -> list[BaseTool]:
    """LangChain tools for the agent: k8s + kb by default, actions only on request."""
    client = mcp_client(mode=mode, fixture=fixture, include_actions=include_actions, run_id=run_id)
    return await client.get_tools()


def tool_text(result: Any) -> str:
    """Normalize a tool result to text.

    langchain-mcp-adapters returns MCP content as a list of blocks
    (``[{"type": "text", "text": ...}]``); older versions returned a plain string.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(getattr(block, "text", block))
            for block in result
        )
    return str(result)
