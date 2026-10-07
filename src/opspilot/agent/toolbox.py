"""Tool access for the agent: MCP sessions (production) or in-process servers (fast replay).

Both expose the same tool schemas, taken from the MCP servers themselves, so the model
sees identical tools either way.
"""

import contextlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from langchain_core.tools import BaseTool
from mcp.server.fastmcp import FastMCP

from opspilot.tools import Mode, mcp_client, tool_text

READ_TOOLS = (
    "list_pods",
    "describe_pod",
    "get_pod_logs",
    "get_events",
    "get_deployment",
    "get_rollout_history",
    "list_services",
    "get_service_endpoints",
    "get_configmap",
    "list_nodes",
    "list_pvcs",
)
KB_TOOLS = ("search_knowledge",)
INVESTIGATION_TOOLS = READ_TOOLS + KB_TOOLS


class ToolBox(Protocol):
    """Named tools with JSON schemas, callable with a dict of arguments."""

    def specs(self, names: Sequence[str]) -> list[dict[str, Any]]: ...

    async def call(self, name: str, args: dict[str, Any]) -> str: ...


def _spec(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": schema},
    }


class InProcessToolBox:
    """Calls the server tool functions directly (no subprocess). Used for replay and tests."""

    def __init__(self, *servers: FastMCP) -> None:
        self._servers = servers
        self._specs: dict[str, dict[str, Any]] = {}
        self._owner: dict[str, FastMCP] = {}

    async def _load(self) -> None:
        if self._specs:
            return
        for server in self._servers:
            for tool in await server.list_tools():
                self._specs[tool.name] = _spec(tool.name, tool.description or "", tool.inputSchema)
                self._owner[tool.name] = server

    def specs(self, names: Sequence[str]) -> list[dict[str, Any]]:
        if not self._specs:
            raise RuntimeError("open the toolbox first: async with InProcessToolBox(...)")
        return [self._specs[n] for n in names if n in self._specs]

    async def call(self, name: str, args: dict[str, Any]) -> str:
        await self._load()
        if name not in self._owner:
            return f'{{"error":{{"type":"unknown_tool","message":"No tool named {name}."}}}}'
        result = await self._owner[name].call_tool(name, args)
        blocks: Any = result[0] if isinstance(result, tuple) else result
        return tool_text([{"text": getattr(b, "text", "")} for b in blocks])

    async def __aenter__(self) -> "InProcessToolBox":
        await self._load()
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class MCPToolBox:
    """Persistent stdio sessions to the MCP servers for the length of one run."""

    def __init__(
        self,
        *,
        mode: Mode,
        fixture: Path | None = None,
        include_actions: bool = False,
        run_id: str | None = None,
    ) -> None:
        self._client = mcp_client(
            mode=mode, fixture=fixture, include_actions=include_actions, run_id=run_id
        )
        self._servers = ["k8s", "kb"] + (["actions"] if include_actions else [])
        self._stack = contextlib.AsyncExitStack()
        self._tools: dict[str, BaseTool] = {}

    async def __aenter__(self) -> "MCPToolBox":
        from langchain_mcp_adapters.tools import load_mcp_tools

        for server in self._servers:
            session = await self._stack.enter_async_context(self._client.session(server))
            for tool in await load_mcp_tools(session):
                self._tools[tool.name] = tool
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    def specs(self, names: Sequence[str]) -> list[dict[str, Any]]:
        out = []
        for name in names:
            tool = self._tools.get(name)
            if tool is not None:
                schema = tool.args_schema if isinstance(tool.args_schema, dict) else {}
                out.append(_spec(name, tool.description, schema))
        return out

    async def call(self, name: str, args: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f'{{"error":{{"type":"unknown_tool","message":"No tool named {name}."}}}}'
        return tool_text(await tool.ainvoke(args))
