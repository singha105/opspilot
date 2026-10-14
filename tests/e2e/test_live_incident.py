"""End to end on the live demo cluster: fault -> agent -> human approval -> fix -> recovery.

Needs: make cluster-up demo-build demo-deploy rbac-apply secrets, Weaviate with the
knowledge base (make infra-up; opspilot kb ingest), and Ollama with qwen3:4b. Takes about
three minutes. Runs through IncidentService, the path the API and the console use.
"""

import asyncio

import pytest

from opspilot.config import get_settings
from opspilot.faults.injector import Injector
from opspilot.faults.scenario import load_scenarios
from opspilot.kube import admin_client
from opspilot.models.incident import ApprovalDecision
from opspilot.service import IncidentService, load_state

pytestmark = pytest.mark.e2e
SCENARIO = "oom-payments"


def test_live_oom_incident_is_fixed_only_after_approval() -> None:
    settings = get_settings()
    scenario = load_scenarios(settings.scenarios_dir)[SCENARIO]
    injector = Injector(
        admin_client(settings.admin_context),
        context=settings.admin_context,
        base_dir=settings.demo_base_dir,
    )
    service = IncidentService(settings)
    injector.inject(scenario)
    try:
        injector.wait_for_symptom(scenario)
        incident = service.create(mode="live", source="e2e")
        paused = asyncio.run(service.run(incident, scenario.alert.model_dump()))
        assert paused.status == "awaiting_approval", paused.state
        assert paused.pending is not None
        before = load_state(paused)
        assert before is not None
        assert before.action_result is None  # nothing executed before the decision
        assert injector.symptom_present(scenario)  # and the fault is still there

        approval = ApprovalDecision(decision="approve", approver="e2e-test", reason="e2e")
        done = load_state(asyncio.run(service.decide(incident, approval)))
        assert done is not None
        assert done.diagnosis is not None
        assert done.diagnosis.root_cause_category.value == "OOM_KILLED"
        assert done.action_result is not None
        assert done.action_result.executed
        assert done.verification is not None
        assert done.verification.status == "resolved", done.verification.details
    finally:
        injector.reset(scenario)
