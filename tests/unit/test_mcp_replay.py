"""Replay mode, checked against the committed fixtures (no cluster needed)."""

import asyncio
import json
from pathlib import Path

import pytest

from opspilot.mcp_servers.common import AuditLog, ToolRunner
from opspilot.mcp_servers.k8s_readonly.replay import ReplayBackend, bind_args, call_key
from opspilot.mcp_servers.k8s_readonly.server import DESCRIPTIONS, K8sTools, build_server

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "evals" / "fixtures"
RECORDED = sorted(p.stem for p in FIXTURES.glob("*.json"))


def replay_tools(fixture: str, tmp_path: Path) -> K8sTools:
    backend = ReplayBackend(FIXTURES / f"{fixture}.json")
    runner = ToolRunner(AuditLog(tmp_path / "audit.jsonl", "opspilot-k8s", "replay"))
    return K8sTools(backend, runner, ["shop"])  # type: ignore[arg-type]


def test_fixture_metadata() -> None:
    assert "healthy" in RECORDED
    for path in FIXTURES.glob("*.json"):
        meta = json.loads(path.read_text())["metadata"]
        assert meta["fixture_id"] == path.stem
        assert meta["kubernetes_version"].startswith("v1.")
        assert meta["identity"] == "opspilot-reader"
        assert meta["tool_calls"] == len(json.loads(path.read_text())["calls"])


@pytest.mark.parametrize("fixture", RECORDED)
def test_every_recorded_call_replays_identically(fixture: str, tmp_path: Path) -> None:
    tools = replay_tools(fixture, tmp_path)
    calls = json.loads((FIXTURES / f"{fixture}.json").read_text())["calls"]
    covered = set()
    for key, recorded in calls.items():
        spec = json.loads(key)
        text = getattr(tools, spec["tool"])(**spec["args"])
        assert json.loads(text) == recorded, key
        covered.add(spec["tool"])
    assert covered == set(DESCRIPTIONS)  # all 11 tools are recorded in every fixture


def test_derived_logs_and_events(tmp_path: Path) -> None:
    tools = replay_tools("missing-env-inventory", tmp_path)
    pod = next(
        p["name"]
        for p in json.loads(tools.list_pods("shop"))["pods"]
        if p["name"].startswith("inventory")
    )
    logs = json.loads(tools.get_pod_logs("shop", pod, previous=True, tail_lines=5))
    assert logs["tail_lines"] == 5
    assert len(logs["lines"]) <= 5
    fatal = json.loads(
        tools.get_pod_logs("shop", pod, container="app", previous=True, contains="fatal")
    )
    assert fatal["lines"]
    assert all("fatal" in line.lower() for line in fatal["lines"])
    events = json.loads(tools.get_events("shop", since_minutes=10))
    assert all(e["age_s"] <= 600 for e in events["events"])
    per_object = json.loads(
        tools.get_events("shop", involved_object_name="inventory-api", since_minutes=60)
    )
    assert all(e["object"].endswith("/inventory-api") for e in per_object["events"])


def test_unrecorded_call_suggests_broader_calls(tmp_path: Path) -> None:
    tools = replay_tools("oom-payments", tmp_path)
    out = json.loads(tools.describe_pod("shop", "payments-api-does-not-exist"))
    assert out["error"]["type"] == "not_recorded"
    assert "list_pods" in out["error"]["hint"]
    assert json.loads(tools.list_pods("kube-system"))["error"]["type"] == "namespace_not_allowed"
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert {r["mode"] for r in records} == {"replay"}
    assert [r["status"] for r in records] == ["error", "rejected"]


def test_replay_server_over_mcp(tmp_path: Path) -> None:
    server = build_server(replay_tools("oom-payments", tmp_path))
    content = asyncio.run(server.call_tool("list_pods", {"namespace": "shop"}))
    blocks = content if isinstance(content, list) else content[0]
    assert "OOMKilled" in blocks[0].text


def test_canonical_keys() -> None:
    assert call_key("list_pods", {"namespace": "shop", "label_selector": None}) == call_key(
        "list_pods", bind_args("list_pods", "shop")
    )
    assert bind_args("get_pod_logs", "shop", "p") == {
        "namespace": "shop",
        "name": "p",
        "container": None,
        "previous": False,
        "tail_lines": 100,
        "contains": None,
    }
    with pytest.raises(AttributeError):
        ReplayBackend(FIXTURES / "healthy.json").delete_pod  # noqa: B018
