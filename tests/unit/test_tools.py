import asyncio
import json
from pathlib import Path

import pytest

from opspilot.tools import load_tools, server_connections, tool_text

FIXTURE = Path(__file__).parents[2] / "evals" / "fixtures" / "oom-payments.json"


def test_default_servers_are_read_only() -> None:
    conns = server_connections(run_id="run-x")
    assert set(conns) == {"k8s", "kb"}
    assert conns["k8s"]["args"][-2:] == ["--mode", "live"]
    assert conns["k8s"]["env"]["OPSPILOT_RUN_ID"] == "run-x"  # type: ignore[index]
    assert conns["kb"]["env"]["OPSPILOT_RUN_ID"] == "run-x"  # type: ignore[index]


def test_actions_only_when_requested_and_never_in_replay() -> None:
    assert "actions" in server_connections(include_actions=True)
    with pytest.raises(ValueError, match="live-only"):
        server_connections(mode="replay", fixture=FIXTURE, include_actions=True)
    with pytest.raises(ValueError, match="fixture"):
        server_connections(mode="replay")


def test_replay_tools_through_langchain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSPILOT_RUNS_DIR", str(tmp_path))

    async def run() -> tuple[set[str], str]:
        tools = {
            t.name: t for t in await load_tools(mode="replay", fixture=FIXTURE, run_id="run-t")
        }
        return set(tools), tool_text(await tools["list_pods"].ainvoke({"namespace": "shop"}))

    names, text = asyncio.run(run())
    assert len(names) == 13
    assert "execute_action" not in names
    assert any(p.get("last_reason") == "OOMKilled" for p in json.loads(text)["pods"])
    audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert audit[-1]["mode"] == "replay"
    assert audit[-1]["run_id"] == "run-t"


def test_tool_text_normalizes_content_blocks() -> None:
    assert tool_text("plain") == "plain"
    assert tool_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"
    assert tool_text(5) == "5"
