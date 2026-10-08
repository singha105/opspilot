import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from agent_fakes import ScriptedChatModel, replay_servers
from test_agent_graph import FOLLOW_UPS, PATCH_CHOICE, script

from opspilot.agent.observability import RunLog
from opspilot.agent.state import IncidentState, SecurityFlag
from opspilot.agent.toolbox import InProcessToolBox
from opspilot.config import Settings
from opspilot.evals import agent_eval as ae
from opspilot.evals.scoring import EvalCase, RunOutcome
from opspilot.models import RootCauseCategory
from opspilot.models.incident import (
    ActionResult,
    ApprovalDecision,
    Diagnosis,
    Evidence,
    RemediationProposal,
)
from opspilot.rag.models import RetrievedChunk

ROOT = Path(__file__).parents[2]
CASE = EvalCase(
    id="oom-payments",
    split="dev",
    category=RootCauseCategory.OOM_KILLED,
    component="payments-api",
    runbook_ids=["rb-oom-killed"],
    acceptable_actions=["patch_container_resources", "rollback_deployment"],
)


def settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_dir=tmp_path / "data",
        runs_dir=tmp_path / "runs",
        approval_secret="k" * 64,
    )


def chunk(cite: str, doc: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cite,
        doc_id=doc,
        doc_type="runbook",
        title="t",
        section="s",
        text="x",
        score=1,
        rank=1,
        citation_id=cite,
    )


def test_outcome_from_state() -> None:
    state = IncidentState(
        incident_id="i",
        retrieved=[chunk("R1", "rb-oom-killed"), chunk("R2", "rb-bad-rollout")],
        evidence=[Evidence(id="E1", tool="list_pods", summary="s")],
        diagnosis=Diagnosis(
            root_cause_category="OOM_KILLED",
            component="payments-api",
            summary="killed [E1] per [R1]; see [R9]",
            evidence_refs=["E1"],
            runbook_refs=["R1"],
            confidence=0.9,
        ),
        proposal=RemediationProposal(
            kind="action", action={"type": "rollback_deployment"}, rationale="r"
        ),
        approval=ApprovalDecision(decision="approve", approver="eval-harness"),
        action_result=ActionResult(executed=False, simulated=True),
        security_flags=[
            SecurityFlag(source="get_pod_logs", pattern="ignore_previous", excerpt="x")
        ],
    )
    out = ae.outcome_from_state(state, 12.5)
    assert out.category == "OOM_KILLED"
    assert out.cited_ids == ["E1", "R1", "R9"]
    assert out.cited_doc_ids == ["rb-oom-killed"]
    assert out.valid_ids == ["E1", "R1", "R2"]
    assert out.retrieved_doc_ids == ["rb-oom-killed", "rb-bad-rollout"]
    assert out.executed  # a simulated execution still counts as an action taken
    assert out.flag_sources == ["get_pod_logs"]
    assert out.latency_s == 12.5


def test_cache_key_changes_with_code_config_and_fixture(tmp_path: Path) -> None:
    c3, c2 = ae.CONFIGS["C3"], ae.CONFIGS["C2"]
    base = ae.cache_key("a", c3, code="x", fixture_sha="f", model="m")
    assert base == ae.cache_key("a", c3, code="x", fixture_sha="f", model="m")
    assert base != ae.cache_key("a", c2, code="x", fixture_sha="f", model="m")
    assert base != ae.cache_key("a", c3, code="y", fixture_sha="f", model="m")
    assert base != ae.cache_key("a", c3, code="x", fixture_sha="g", model="m")
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "a.py").write_text("x = 1\n")
    first = ae.code_hash(tmp_path, ["agent"])
    (tmp_path / "agent" / "a.py").write_text("x = 2\n")
    assert ae.code_hash(tmp_path, ["agent"]) != first


def test_settings_for_each_config() -> None:
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    c1 = ae.settings_for(ae.CONFIGS["C1"], base)
    assert (c1.default_store, c1.retrieval_mode, c1.agent_rerank) == ("chroma", "dense", False)
    c3 = ae.settings_for(ae.CONFIGS["C3"], base)
    assert (c3.default_store, c3.retrieval_mode, c3.agent_rerank) == ("weaviate", "hybrid", True)
    assert not ae.CONFIGS["C0"].use_rag
    assert ae.DEFAULT_CONFIG == "C3"


def test_evaluate_caches_and_resumes(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_run(case: EvalCase, config: ae.EvalConfig, run_id: str) -> RunOutcome:
        calls.append(case.id)
        return RunOutcome(
            category="OOM_KILLED",
            component="payments-api",
            proposal_kind="action",
            action={"type": "rollback_deployment", "deployment": "payments-api"},
            approval_decision="approve",
            executed=True,
            latency_s=3.0,
        )

    s = settings(tmp_path).model_copy(update={"fixtures_dir": ROOT / "evals" / "fixtures"})
    results = tmp_path / "results"
    first = ae.AgentEval(s, ROOT, results, resume=True, run=fake_run, today="2026-10-08")
    rows = first.evaluate([CASE], ae.CONFIGS["C3"])
    assert calls == ["oom-payments"]
    assert rows[0].category_correct
    assert rows[0].remediation_acceptable
    path = results / "agent-2026-10-08-C3.jsonl"
    assert json.loads(path.read_text())["scenario_id"] == "oom-payments"

    again = ae.AgentEval(s, ROOT, results, resume=True, run=fake_run, today="2026-10-08")
    seen: list[bool] = []
    again.evaluate([CASE], ae.CONFIGS["C3"], on_row=lambda row, cached: seen.append(cached))
    assert calls == ["oom-payments"]  # served from the cache
    assert seen == [True]

    fresh = ae.AgentEval(s, ROOT, results, resume=False, run=fake_run, today="2026-10-08")
    fresh.evaluate([CASE], ae.CONFIGS["C3"])
    assert calls == ["oom-payments", "oom-payments"]  # --resume off reruns
    assert [r.scenario_id for r in ae.read_rows(path)] == ["oom-payments"]


def test_run_case_replays_a_full_incident(tmp_path: Path) -> None:
    diagnosis = {
        "root_cause_category": "OOM_KILLED",
        "component": "payments-api",
        "summary": "payments-api is OOMKilled at startup [E2].",
        "evidence_refs": ["E1", "E2"],
        "runbook_refs": [],
        "confidence": 0.9,
        "alternatives": [],
    }
    model = ScriptedChatModel(replies=script(diagnosis, PATCH_CHOICE, FOLLOW_UPS))

    @contextlib.asynccontextmanager
    async def toolbox(fixture: Path, config: ae.EvalConfig, s: Settings) -> AsyncIterator[Any]:
        async with InProcessToolBox(*replay_servers("oom-payments", tmp_path)) as box:
            yield box

    alert = ae.case_alert(CASE, ROOT / "faults" / "scenarios")
    out = asyncio.run(
        ae.run_case(
            alert,
            ROOT / "evals" / "fixtures" / "oom-payments.json",
            ae.CONFIGS["C0"],
            settings(tmp_path),
            run_id="eval-test",
            llm=lambda role: model,
            toolbox_factory=toolbox,
            events=RunLog(tmp_path / "runs", "eval-test"),
        )
    )
    saved = json.loads((tmp_path / "runs" / "eval-test" / "state.json").read_text())
    assert saved["status"] == "reported"
    assert saved["diagnosis"]["root_cause_category"] == "OOM_KILLED"
    assert out.category == "OOM_KILLED"
    assert out.proposal_kind == "action"
    assert out.approval_decision == "approve"
    assert out.executed  # simulated: replay never executes
    assert not out.use_rag
    assert out.tool_calls == 2


def test_healthy_case_uses_the_false_alarm_alert() -> None:
    healthy = EvalCase(
        id="healthy", split="control", category=RootCauseCategory.UNKNOWN, control=True
    )
    alert = ae.case_alert(healthy, ROOT / "faults" / "scenarios")
    assert alert["labels"]["namespace"] == "shop"


def test_harness_approves_only_acceptable_fixes_for_the_true_component() -> None:
    from opspilot.evals.live_eval import harness_decision

    def pending(kind: str, action: dict[str, Any] | None) -> dict[str, Any]:
        return {"proposal": {"kind": kind, "action": action}}

    patch = {"type": "patch_container_resources", "deployment": "payments-api"}
    ok = harness_decision(CASE, pending("action", patch))
    assert (ok.decision, ok.approver) == ("approve", "eval-harness")
    wrong_type = harness_decision(CASE, pending("action", {**patch, "type": "scale_deployment"}))
    assert wrong_type.decision == "reject"
    assert "not an acceptable fix" in wrong_type.reason
    wrong_target = harness_decision(CASE, pending("action", {**patch, "deployment": "orders-api"}))
    assert wrong_target.decision == "reject"
    assert "not the faulty component" in wrong_target.reason
    manual = harness_decision(CASE, pending("manual_change", None))
    assert manual.decision == "reject"
