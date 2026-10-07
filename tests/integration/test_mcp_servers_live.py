"""The read-only MCP servers against the live demo cluster.

Needs: make cluster-up demo-deploy rbac-apply (fresh kubeconfigs in .secrets/).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from opspilot.config import get_settings
from opspilot.mcp_servers.common import AuditLog, ToolRunner
from opspilot.mcp_servers.common.kube import apis_from_kubeconfig
from opspilot.mcp_servers.k8s_readonly.backend import K8sReadBackend
from opspilot.mcp_servers.k8s_readonly.server import K8sTools

pytestmark = pytest.mark.integration
SETTINGS = get_settings()
ROOT = Path(__file__).parents[2]


def _tools(kubeconfig: Path, tmp_path: Path) -> K8sTools:
    backend = K8sReadBackend(apis_from_kubeconfig(kubeconfig))
    runner = ToolRunner(AuditLog(tmp_path / "audit.jsonl", "opspilot-k8s", "live"))
    return K8sTools(backend, runner, SETTINGS.allowed_namespaces)


def test_all_eleven_tools_live(tmp_path: Path) -> None:
    t = _tools(SETTINGS.reader_kubeconfig, tmp_path)
    pods = json.loads(t.list_pods("shop"))["pods"]
    assert {p["name"].rsplit("-", 2)[0] for p in pods} >= {"payments-api", "orders-api", "redis"}
    pod = next(p["name"] for p in pods if p["name"].startswith("payments-api"))
    results = {
        "describe_pod": t.describe_pod("shop", pod),
        "get_pod_logs": t.get_pod_logs("shop", pod, tail_lines=5),
        "get_events": t.get_events("shop", since_minutes=120),
        "get_deployment": t.get_deployment("shop", "payments-api"),
        "get_rollout_history": t.get_rollout_history("shop", "payments-api"),
        "list_services": t.list_services("shop"),
        "get_service_endpoints": t.get_service_endpoints("shop", "redis"),
        "get_configmap": t.get_configmap("shop", "shopfront-settings"),
        "list_nodes": t.list_nodes(),
        "list_pvcs": t.list_pvcs("shop"),
    }
    for name, text in results.items():
        assert len(text) <= 6000, name
        assert "error" not in json.loads(text), (name, text)
    logs = json.loads(results["get_pod_logs"])
    assert logs["lines"]
    assert all(line.startswith("{") for line in logs["lines"])  # real lines, not a bytes repr
    audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(audit) == 11
    assert {r["status"] for r in audit} == {"ok"}


def test_forbidden_is_graceful(tmp_path: Path) -> None:
    """The operator identity may not list pods: the tool must return a clean 403 error."""
    t = _tools(SETTINGS.operator_kubeconfig, tmp_path)
    out = json.loads(t.list_pods("shop"))
    assert out["error"]["type"] == "forbidden"
    assert out["error"]["status"] == 403
    assert "Traceback" not in json.dumps(out)


def test_stdio_server_end_to_end(tmp_path: Path) -> None:
    async def run() -> tuple[list[str], str]:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "opspilot.mcp_servers.k8s_readonly"],
            cwd=str(ROOT),
            env={**os.environ, "OPSPILOT_RUNS_DIR": str(tmp_path)},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            result = await session.call_tool("list_nodes", {})
            return [t.name for t in listed.tools], result.content[0].text  # type: ignore[union-attr]

    names, text = asyncio.run(run())
    assert len(names) == 11
    assert json.loads(text)["nodes"]
    assert (tmp_path / "audit.jsonl").exists()
