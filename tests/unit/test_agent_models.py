import pytest
from pydantic import ValidationError

from opspilot.agent.state import IncidentState, SecurityFlag
from opspilot.models.incident import (
    ApprovalDecision,
    Diagnosis,
    Evidence,
    RunMetrics,
    Triage,
)


def test_triage_bounds() -> None:
    Triage(
        service="payments-api",
        namespace="shop",
        symptom_summary="pods restart",
        candidate_categories=["OOM_KILLED"],
        search_queries=["exit 137", "oom"],
    )
    with pytest.raises(ValidationError):
        Triage(
            service="x",
            namespace="shop",
            symptom_summary="s",
            candidate_categories=["OOM_KILLED"] * 4,
            search_queries=["a", "b"],
        )
    with pytest.raises(ValidationError):
        Triage(
            service="x",
            namespace="shop",
            symptom_summary="s",
            candidate_categories=["NOT_A_CATEGORY"],  # type: ignore[list-item]
            search_queries=["a", "b"],
        )


def test_diagnosis_matches_example_shape() -> None:
    d = Diagnosis.model_validate(
        {
            "root_cause_category": "OOM_KILLED",
            "component": "payments-api",
            "summary": "OOMKilled at startup [E2].",
            "evidence_refs": ["E2", "E4"],
            "runbook_refs": ["R1"],
            "confidence": 0.86,
            "alternatives": [{"category": "BAD_ROLLOUT", "why_less_likely": "image unchanged"}],
        }
    )
    assert d.alternatives[0].category == "BAD_ROLLOUT"
    with pytest.raises(ValidationError):
        Diagnosis.model_validate({**d.model_dump(), "confidence": 1.5})


def test_evidence_length_limits() -> None:
    with pytest.raises(ValidationError):
        Evidence(id="E1", tool="list_pods", summary="x" * 301)
    with pytest.raises(ValidationError):
        Evidence(id="E1", tool="list_pods", summary="ok", excerpt="y" * 801)


def test_approval_decision_is_strict() -> None:
    ApprovalDecision(decision="approve", approver="arnab")
    with pytest.raises(ValidationError):
        ApprovalDecision(decision="yolo", approver="arnab")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ApprovalDecision(decision="approve", approver="")
    with pytest.raises(ValidationError):
        ApprovalDecision.model_validate({"decision": "approve", "approver": "a", "token": "x"})


def test_run_metrics_usage() -> None:
    m = RunMetrics()
    m.add_usage({"input_tokens": 100, "output_tokens": 20})
    m.add_usage(None)
    assert (m.tokens_in, m.tokens_out, m.llm_calls) == (100, 20, 2)


def test_state_ids_and_escalation() -> None:
    state = IncidentState(
        incident_id="inc-1",
        evidence=[Evidence(id="E1", tool="list_pods", summary="s")],
        security_flags=[SecurityFlag(source="logs", pattern="ignore_previous", excerpt="x")],
    )
    assert state.evidence_ids() == {"E1"}
    assert state.citation_ids() == set()
    assert not state.needs_human
    assert state.model_copy(update={"escalation_reason": "low confidence"}).needs_human
