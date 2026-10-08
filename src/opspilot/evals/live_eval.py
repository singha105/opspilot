"""Live evaluation subset: inject a fault on the demo cluster, run the agent, let the
harness approve only an acceptable action, verify recovery and reset.

Approvals here are made by the harness (approver "eval-harness") and only on the local
demo cluster. The agent's own path is unchanged: the token is still minted by
``agent/approval.py`` after the decision, and the actions server still checks it.
"""

import datetime as dt
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from opspilot.agent import graph as g
from opspilot.agent.deps import AgentDeps
from opspilot.agent.observability import RunLog
from opspilot.agent.toolbox import MCPToolBox
from opspilot.config import Settings
from opspilot.evals.agent_eval import APPROVER, outcome_from_state
from opspilot.evals.scoring import EvalCase, category_correct, component_correct
from opspilot.faults.injector import Injector
from opspilot.faults.scenario import Scenario
from opspilot.llm import LLMPool
from opspilot.models.incident import ApprovalDecision


class LiveRow(BaseModel):
    """One live end-to-end run."""

    scenario_id: str
    config: str
    category_pred: str | None
    category_true: str
    category_correct: bool | None
    component_correct: bool | None
    proposal: str | None
    action: dict[str, Any] | None
    decision: str | None
    decision_reason: str
    executed: bool
    verification: str | None
    recovered: bool
    symptom_s: float
    agent_s: float
    time_to_recovery_s: float | None
    tokens_in: int
    tokens_out: int
    tool_calls: int
    run_id: str


def harness_decision(case: EvalCase, pending: dict[str, Any]) -> ApprovalDecision:
    """Approve only an action that is an acceptable fix for this case's true component."""
    proposal = pending.get("proposal") or {}
    action = proposal.get("action") or {}
    if proposal.get("kind") != "action" or not action:
        reason = "no executable action proposed"
    elif action.get("type") not in case.acceptable_actions:
        reason = f"{action.get('type')} is not an acceptable fix for this case"
    elif action.get("deployment") != case.component:
        reason = f"targets {action.get('deployment')}, not the faulty component"
    else:
        return ApprovalDecision(
            decision="approve", approver=APPROVER, reason="acceptable fix for this case"
        )
    return ApprovalDecision(decision="reject", approver=APPROVER, reason=reason)


async def run_live_case(
    case: EvalCase,
    scenario: Scenario,
    injector: Injector,
    settings: Settings,
    config_name: str,
    runs_dir: Path,
) -> LiveRow:
    """Inject, investigate live, decide, execute, verify and always reset."""
    run_id = f"live-{dt.datetime.now(dt.UTC):%Y%m%d-%H%M%S}-{case.id}"
    injector.inject(scenario)
    try:
        symptom_s = injector.wait_for_symptom(scenario)
        symptom_at = time.time()
        pool = LLMPool(settings)
        decision: ApprovalDecision | None = None
        async with (
            MCPToolBox(mode="live", include_actions=True, run_id=run_id) as toolbox,
            g.sqlite_checkpointer(settings.data_dir / "eval-checkpoints.sqlite") as saver,
        ):
            from opspilot.rag.pipeline import build_retriever

            deps = AgentDeps(
                toolbox=toolbox,
                llm=pool,
                retriever=lambda: build_retriever(rerank=settings.agent_rerank, settings=settings),
                settings=settings,
                events=RunLog(runs_dir, run_id),
                actions=toolbox,
                report_dir=runs_dir,
            )
            graph = g.build_graph(deps, saver)
            try:
                await g.start(graph, run_id, scenario.alert.model_dump(), mode="live", use_rag=True)
                pending = await g.pending_approval(graph, run_id)
                if pending is not None:
                    decision = harness_decision(case, pending)
                    await g.resume(graph, run_id, decision)
                state = await g.current_state(graph, run_id)
            finally:
                await pool.aclose()
        finished_at = time.time()
    finally:
        injector.reset(scenario)
    assert state is not None
    out = outcome_from_state(state, finished_at - symptom_at)
    verification = state.verification.status if state.verification else None
    recovered = verification == "resolved"
    return LiveRow(
        scenario_id=case.id,
        config=config_name,
        category_pred=out.category,
        category_true=case.category.value,
        category_correct=category_correct(case, out),
        component_correct=component_correct(case, out),
        proposal=out.proposal_kind,
        action=out.action,
        decision=decision.decision if decision else None,
        decision_reason=decision.reason if decision else "the run ended before approval",
        executed=bool(state.action_result and state.action_result.executed),
        verification=verification,
        recovered=recovered,
        symptom_s=round(symptom_s, 1),
        agent_s=round(out.latency_s, 1),
        time_to_recovery_s=round(out.latency_s, 1) if recovered else None,
        tokens_in=out.tokens_in,
        tokens_out=out.tokens_out,
        tool_calls=out.tool_calls,
        run_id=run_id,
    )
