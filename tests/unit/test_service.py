import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from agent_fakes import ScriptedChatModel, replay_servers, tool_call

from opspilot.agent.toolbox import InProcessToolBox
from opspilot.config import Settings
from opspilot.models.incident import ApprovalDecision
from opspilot.service import IncidentService, ServiceError, load_state

TRIAGE = {
    "service": "payments-api",
    "namespace": "shop",
    "symptom_summary": "restarts",
    "candidate_categories": ["OOM_KILLED"],
    "search_queries": ["exit code 137", "memory limit"],
}
DIAGNOSIS = {
    "root_cause_category": "OOM_KILLED",
    "component": "payments-api",
    "summary": "OOMKilled at startup [E1].",
    "evidence_refs": ["E1"],
    "runbook_refs": [],
    "confidence": 0.8,
    "alternatives": [],
}
CHOICE = {"action_type": "patch_container_resources", "deployment": "payments-api"}
FOLLOW = {"follow_ups": ["Alert on memory near the limit."]}


def make_service(tmp_path: Path, model: ScriptedChatModel) -> IncidentService:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        runs_dir=tmp_path / "runs",
        data_dir=tmp_path / "data",
        approval_secret="k" * 64,
    )

    @contextlib.asynccontextmanager
    async def toolbox(mode: str, fixture: Path | None, run_id: str) -> AsyncIterator[Any]:
        async with InProcessToolBox(*replay_servers("oom-payments", tmp_path)) as box:
            yield box

    return IncidentService(settings, toolbox_factory=toolbox, llm_factory=lambda role: model)


def test_run_pauses_then_another_service_resumes(tmp_path: Path) -> None:
    model = ScriptedChatModel(
        replies=[
            TRIAGE,
            tool_call("list_pods", {"namespace": "shop"}),
            tool_call("finish_investigation", {"reason": "seen"}),
            DIAGNOSIS,
            CHOICE,
            FOLLOW,
        ]
    )
    svc = make_service(tmp_path, model)
    incident = svc.create(mode="replay", scenario="oom-payments", use_rag=False, source="ui")
    alert, _ = svc.scenario_alert("oom-payments")
    view = asyncio.run(svc.run(incident, alert))
    assert view.status == "awaiting_approval"
    assert view.meta.source == "ui"
    assert view.pending is not None
    assert view.pending["proposal"]["action"]["type"] == "patch_container_resources"
    assert [r.incident_id for r in svc.list()] == [incident]

    other = make_service(tmp_path, model)  # a different process, e.g. the API after the UI
    decision = ApprovalDecision(decision="approve", approver="sam", reason="ok")
    done = asyncio.run(other.decide(incident, decision))
    assert done.pending is None
    assert done.status == "reported"
    assert done.report_markdown is not None
    state = load_state(done)
    assert state is not None
    assert state.approval is not None
    assert state.approval.approver == "sam"
    with pytest.raises(ServiceError, match="not waiting for approval"):
        asyncio.run(other.decide(incident, decision))


def test_create_validates_requests(tmp_path: Path) -> None:
    svc = make_service(tmp_path, ScriptedChatModel())
    with pytest.raises(ServiceError, match="unknown scenario"):
        svc.create(mode="replay", scenario="no-such-fault")
    with pytest.raises(ServiceError, match="replay needs a recorded fixture"):
        svc.create(mode="replay")
    live = svc.create(mode="live")
    assert svc.get(live).status == "running"
    assert svc.get(live).meta.fixture is None
    with pytest.raises(ServiceError, match="no incident"):
        svc.get("missing")


def test_scenarios_lists_only_recorded_ones(tmp_path: Path) -> None:
    svc = make_service(tmp_path, ScriptedChatModel())
    assert "oom-payments" in svc.scenarios()
    assert len(svc.scenarios()) == 30
