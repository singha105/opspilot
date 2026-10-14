from typing import Any

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from opspilot.agent.state import IncidentState
from opspilot.config import Settings
from opspilot.models import Alert
from opspilot.models.incident import (
    ApprovalDecision,
    Diagnosis,
    Evidence,
    RemediationProposal,
    Triage,
)
from opspilot.rag.models import RetrievedChunk
from opspilot.service import IncidentView, RunMeta
from opspilot.ui import app as ui_app
from opspilot.ui.views import (
    StatusSink,
    call_text,
    clean_section,
    diff_rows,
    escape_md,
    link_refs,
    status_label,
)


def test_untrusted_text_is_escaped_and_refs_become_anchors() -> None:
    assert escape_md("[click](http://evil)") == "\\[click\\]\\(http://evil\\)"
    linked = link_refs("Killed [E2] per [R1]; see [docs](http://x)")
    assert "[[E2]](#e2)" in linked
    assert "[[R1]](#r1)" in linked
    assert "](http://x)" not in linked  # the planted link stays literal text


def test_small_helpers() -> None:
    assert clean_section("How Pods handle problems {#container-restarts}") == (
        "How Pods handle problems"
    )
    assert call_text("get_pod_logs", {"name": "p", "previous": False, "tail": None}) == (
        "get_pod_logs(name=p)"
    )
    plan = {"diff": [{"path": "limits.memory", "before": "128Mi", "after": "512Mi"}]}
    assert diff_rows(plan) == [{"field": "limits.memory", "before": "128Mi", "after": "512Mi"}]
    assert status_label("awaiting_approval") == "Waiting for approval"
    assert status_label("odd_state") == "Odd state"


def test_status_sink_streams_progress() -> None:
    lines: list[str] = []

    class Box:
        def update(self, **kw: Any) -> None:
            lines.append(f"label:{kw['label']}")

        def write(self, text: str) -> None:
            lines.append(text)

    sink = StatusSink(Box())
    sink.emit("node_start", node="triage")
    sink.emit("node_end", node="triage", ms=1500, tool_calls=0)
    sink.emit("tool_call", tool="list_pods", args={"namespace": "shop"})
    assert lines == ["label:Running triage…", "✓ `triage` 1.5 s", "  · `list_pods(namespace=shop)`"]


# ---- the console against a fake service ------------------------------------------------------


def paused_state() -> dict[str, Any]:
    state = IncidentState(
        incident_id="inc-ui",
        alert=Alert(name="KubePodCrashLooping", severity="critical", summary="payments restarting"),
        triage=Triage(
            service="payments-api",
            namespace="shop",
            symptom_summary="restarts",
            candidate_categories=["OOM_KILLED"],
            search_queries=["exit 137", "memory"],
        ),
        retrieved=[
            RetrievedChunk(
                chunk_id="c1",
                doc_id="rb-oom-killed",
                doc_type="runbook",
                title="Container OOMKilled",
                section="Symptoms {#symptoms}",
                text="exit code 137",
                score=0.9,
                rank=1,
                citation_id="R1",
            )
        ],
        evidence=[Evidence(id="E1", tool="list_pods", args={"namespace": "shop"}, summary="1 pod")],
        diagnosis=Diagnosis(
            root_cause_category="OOM_KILLED",
            component="payments-api",
            summary="OOMKilled [E1] per [R1].",
            evidence_refs=["E1"],
            runbook_refs=["R1"],
            confidence=0.9,
        ),
        proposal=RemediationProposal(
            kind="action",
            action={"type": "patch_container_resources", "memory_limit": "512Mi"},
            rationale="killed above 128Mi [E1]",
            plan={"diff": [{"path": "limits.memory", "before": "128Mi", "after": "512Mi"}]},
            rollback_plan="patch back to 128Mi",
        ),
    )
    return {**state.model_dump(mode="json"), "status": "awaiting_approval"}


class FakeService:
    def __init__(self) -> None:
        self.settings = Settings(_env_file=None)  # type: ignore[call-arg]
        self.decisions: list[ApprovalDecision] = []
        self.created: list[dict[str, Any]] = []

    def scenarios(self) -> dict[str, Any]:
        return {"oom-payments": type("S", (), {"title": "payments-api OOMKilled"})()}

    def scenario_alert(self, scenario: str) -> tuple[dict[str, Any], None]:
        return {"name": "KubePodCrashLooping", "summary": "payments restarting"}, None

    def create(self, **kw: Any) -> str:
        self.created.append(kw)
        return "inc-ui"

    async def run(self, incident_id: str, alert: Any, sinks: Any = ()) -> None:
        for sink in sinks:
            sink.emit("node_start", node="triage")

    async def decide(self, incident_id: str, decision: ApprovalDecision, sinks: Any = ()) -> None:
        self.decisions.append(decision)

    def get(self, incident_id: str) -> IncidentView:
        return IncidentView(
            incident_id=incident_id,
            status="awaiting_approval",
            meta=RunMeta(mode="replay", scenario="oom-payments", source="ui"),
            state=paused_state(),
            pending={"proposal": {}},
        )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeService:
    service = FakeService()
    monkeypatch.setattr(ui_app, "service_factory", lambda: service)
    st.cache_resource.clear()
    return service


def incidents() -> None:
    from opspilot.ui import app

    app.incidents_page()


def test_investigate_then_approve(fake: FakeService) -> None:
    at = AppTest.from_function(incidents, default_timeout=20).run()
    assert "REPLAY mode" in at.info[0].value
    at.button[0].click().run()  # Investigate
    assert fake.created == [{"mode": "replay", "scenario": "oom-payments", "source": "ui"}]
    text = " ".join(m.value for m in at.markdown)
    assert "**OOM_KILLED** in `payments-api`" in text
    assert "[[E1]](#e1)" in text
    at.text_input[0].input("sam")
    approve = next(b for b in at.button if b.label == "Approve")
    approve.click().run()
    assert [(d.decision, d.approver) for d in fake.decisions] == [("approve", "sam")]


def test_reject_needs_an_approver(fake: FakeService) -> None:
    at = AppTest.from_function(incidents, default_timeout=20).run()
    at.button[0].click().run()
    reject = next(b for b in at.button if b.label == "Reject")
    reject.click().run()
    assert fake.decisions == []
    assert "Enter the approver's name." in [e.value for e in at.error]
    at.text_input[0].input("sam")
    at.text_input[1].input("too risky")
    next(b for b in at.button if b.label == "Reject").click().run()
    assert [(d.decision, d.reason) for d in fake.decisions] == [("reject", "too risky")]


def test_evals_page_renders_report_blocks(fake: FakeService) -> None:
    def evals() -> None:
        from opspilot.ui import app

        app.evals_page()

    at = AppTest.from_function(evals, default_timeout=20).run()
    text = " ".join(m.value for m in at.markdown)
    assert "Root-cause category accuracy" in text
    assert not at.exception


def test_runs_page_lists_and_opens_runs(fake: FakeService, monkeypatch: pytest.MonkeyPatch) -> None:
    from opspilot.agent.observability import RunSummary

    summary = RunSummary(
        incident_id="inc-ui",
        status="awaiting_approval",
        mode="replay",
        alert="payments restarting",
        category="OOM_KILLED",
        confidence=0.9,
        action="patch_container_resources",
        updated_at="2026-10-14T10:00:00+00:00",
    )
    monkeypatch.setattr(fake, "list", lambda: [summary], raising=False)

    def runs() -> None:
        from opspilot.ui import app

        app.runs_page()

    at = AppTest.from_function(runs, default_timeout=20).run()
    assert not at.exception
    assert at.selectbox[0].value == "inc-ui"
    assert any("**OOM_KILLED** in `payments-api`" in m.value for m in at.markdown)


def test_live_mode_investigates_a_pasted_alert(
    fake: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.settings = Settings(_env_file=None, api_mode="live")  # type: ignore[call-arg]
    at = AppTest.from_function(incidents, default_timeout=20).run()
    assert "LIVE mode" in at.info[0].value
    at.text_area[0].input('{"name": "KubePodNotReady", "summary": "orders down"}').run()
    next(b for b in at.button if b.label == "Investigate").click().run()
    assert fake.created == [{"mode": "live", "scenario": None, "source": "ui"}]
