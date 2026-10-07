"""Smoke test of the MCP tools through LangChain, in live and replay mode.

Usage:
  uv run python scripts/mcp_smoke.py --mode replay --fixture evals/fixtures/oom-payments.json
  uv run python scripts/mcp_smoke.py --mode live      # needs the cluster and Weaviate
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from opspilot.tools import load_tools, tool_text


def _short(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


async def call(tool: object, args: dict[str, object]) -> str:
    return tool_text(await tool.ainvoke(args))  # type: ignore[attr-defined]


async def run(mode: str, fixture: Path | None) -> int:
    tools = {t.name: t for t in await load_tools(mode=mode, fixture=fixture)}  # type: ignore[arg-type]
    print(f"{mode}: {len(tools)} tools -> {', '.join(sorted(tools))}")

    pods = json.loads(await call(tools["list_pods"], {"namespace": "shop"}))
    print("list_pods:", [(p["name"], p["ready"], p.get("reason")) for p in pods["pods"]])
    ranked = sorted(pods["pods"], key=lambda p: (p.get("reason") is None, p["ready"][0] != "0"))
    unhealthy = ranked[0]  # prefer a pod with a failure reason, then any NotReady pod
    detail = json.loads(
        await call(tools["describe_pod"], {"namespace": "shop", "name": unhealthy["name"]})
    )
    container = detail["containers"][0]
    print("describe_pod:", unhealthy["name"], container["state"], container["lastState"])
    events = await call(tools["get_events"], {"namespace": "shop", "since_minutes": 30})
    print("get_events:", _short(events))
    last = container["lastState"].get("terminated", {})
    query = (
        " ".join(
            str(x) for x in (unhealthy.get("reason"), last.get("reason"), last.get("exitCode")) if x
        )
        or f"{unhealthy['name']} not ready"
    )
    hits = json.loads(await call(tools["search_knowledge"], {"query": query, "k": 3}))
    print(
        "search_knowledge:",
        [(c["citation_id"], c["doc_id"]) for c in hits.get("chunks", [])] or hits,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["live", "replay"], default="replay")
    parser.add_argument("--fixture", type=Path, default=Path("evals/fixtures/oom-payments.json"))
    args = parser.parse_args()
    fixture = args.fixture if args.mode == "replay" else None
    return asyncio.run(run(args.mode, fixture))


if __name__ == "__main__":
    sys.exit(main())
