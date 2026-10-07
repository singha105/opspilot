import asyncio
import builtins
import json
from pathlib import Path
from typing import Any

import pytest
from agent_fakes import ScriptedChatModel, StubRetriever, replay_servers, tool_call

from opspilot.agent import graph as g
from opspilot.agent.deps import AgentDeps
from opspilot.agent.observability import RunLog, list_runs, run_status, setup_tracing
from opspilot.agent.state import IncidentState
from opspilot.agent.toolbox import InProcessToolBox
from opspilot.config import Settings
from opspilot.models.incident import Diagnosis, IncidentReport


def test_run_log_writes_events_and_state(tmp_path: Path) -> None:
    run = RunLog(tmp_path, "inc-1")
    run.emit("node_start", node="triage")
    run.emit("tool_call", tool="list_pods", args={"namespace": "shop"})
    assert [e["kind"] for e in run.events()] == ["node_start", "tool_call"]
    state = IncidentState(incident_id="inc-1", raw_alert={"secretish": "x"})
    run.write_state(state, "incomplete")
    saved = json.loads(run.state_path.read_text())
    assert saved["status"] == "incomplete"
    assert "raw_alert" not in saved


def test_list_runs_and_status(tmp_path: Path) -> None:
    diag = Diagnosis(
        root_cause_category="OOM_KILLED",
        component="p",
        summary="s [E1]",
        evidence_refs=["E1"],
        confidence=0.9,
    )
    report = IncidentReport(path="r", markdown="m", needs_human=False)
    done = IncidentState(incident_id="inc-a", diagnosis=diag, report=report)
    RunLog(tmp_path, "inc-a").write_state(done, run_status(done, paused=False))
    waiting = IncidentState(incident_id="inc-b")
    RunLog(tmp_path, "inc-b").write_state(waiting, run_status(waiting, paused=True))
    runs = {r.incident_id: r for r in list_runs(tmp_path)}
    assert runs["inc-a"].status == "reported"
    assert runs["inc-a"].category == "OOM_KILLED"
    assert runs["inc-b"].status == "awaiting_approval"
    escalated = done.model_copy(update={"escalation_reason": "low"})
    assert run_status(escalated, paused=False) == "escalated"
    assert run_status(IncidentState(incident_id="x"), paused=False) == "incomplete"


def test_tracing_is_off_by_default_and_tolerates_missing_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert setup_tracing(Settings(_env_file=None)) is False
    real_import = builtins.__import__

    def no_phoenix(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith(("phoenix", "openinference")):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_phoenix)
    assert setup_tracing(Settings(_env_file=None, tracing=True)) is False  # type: ignore[call-arg]


def test_graph_run_emits_node_prompt_and_tool_events(tmp_path: Path) -> None:
    run = RunLog(tmp_path / "runs", "inc-e")
    triage = {
        "service": "payments-api",
        "namespace": "shop",
        "symptom_summary": "s",
        "candidate_categories": ["UNKNOWN"],
        "search_queries": ["a b", "c d"],
    }
    replies = [
        triage,
        tool_call("list_pods", {"namespace": "shop"}),
        tool_call("finish_investigation", {"reason": "nothing wrong"}),
        {
            "root_cause_category": "UNKNOWN",
            "component": "payments-api",
            "summary": "No fault [E1].",
            "evidence_refs": ["E1"],
            "runbook_refs": [],
            "confidence": 0.1,
            "alternatives": [],
        },
        {"follow_ups": ["Tune the alert."]},
    ]
    alert = {
        "name": "A",
        "severity": "warning",
        "summary": "check payments-api",
        "labels": {"namespace": "shop", "deployment": "payments-api"},
    }

    async def go() -> None:
        async with InProcessToolBox(*replay_servers("healthy", tmp_path)) as box:
            model = ScriptedChatModel(replies=replies)
            deps = AgentDeps(
                toolbox=box,
                llm=lambda r: model,
                retriever=StubRetriever,
                events=run,
                report_dir=tmp_path / "runs",
            )
            await g.start(g.build_graph(deps), "inc-e", alert, mode="replay", use_rag=True)

    asyncio.run(go())
    events = run.events()
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "node_start"
    assert {"prompt", "tool_call", "retrieval", "node_end"} <= set(kinds)
    prompts = {e["node"]: e["version"] for e in events if e["kind"] == "prompt"}
    assert prompts["triage"] == "triage-v1"
    assert all(len(e["sha256"]) == 12 for e in events if e["kind"] == "prompt")
    triage_end = next(e for e in events if e["kind"] == "node_end" and e["node"] == "triage")
    assert triage_end["tokens_in"] == 100
    investigate_end = next(
        e for e in events if e["kind"] == "node_end" and e["node"] == "investigate"
    )
    assert investigate_end["tool_calls"] == 1
    assert (tmp_path / "runs" / "inc-e" / "report.md").exists()
