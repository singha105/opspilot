"""The OpsPilot state machine (LangGraph) with a SQLite checkpointer.

ingest_alert -> triage -> retrieve -> investigate -> diagnose
  diagnose:            escalated -> report        | otherwise -> propose_remediation
  propose_remediation: an action -> human_approval | manual change / none -> report
  human_approval:      approve or edit -> execute  | reject -> report
  execute -> verify -> report -> END
"""

import contextlib
import datetime as dt
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from opspilot.agent.deps import AgentDeps
from opspilot.agent.nodes import investigation as inv
from opspilot.agent.nodes import resolution as res
from opspilot.agent.state import IncidentState
from opspilot.models.incident import ApprovalDecision

Node = Callable[[IncidentState, AgentDeps], Awaitable[dict[str, Any]]]
NODES: list[tuple[str, Node]] = [
    ("ingest_alert", inv.ingest_alert),
    ("triage", inv.triage),
    ("retrieve", inv.retrieve),
    ("investigate", inv.investigate),
    ("diagnose", res.diagnose),
    ("propose_remediation", res.propose_remediation),
    ("human_approval", res.human_approval),
    ("execute", res.execute),
    ("verify", res.verify),
    ("report", res.report),
]


def _wrap(
    name: str, fn: Node, deps: AgentDeps
) -> Callable[[IncidentState], Awaitable[dict[str, Any]]]:
    async def node(state: IncidentState) -> dict[str, Any]:
        deps.events.emit("node_start", node=name)
        start = time.perf_counter()
        update = await fn(state, deps)
        elapsed = round((time.perf_counter() - start) * 1000, 1)
        metrics = (update.get("metrics") or state.metrics).model_copy(deep=True)
        metrics.node_latency_ms[name] = metrics.node_latency_ms.get(name, 0.0) + elapsed
        deps.events.emit(
            "node_end",
            node=name,
            ms=elapsed,
            tokens_in=metrics.tokens_in - state.metrics.tokens_in,
            tokens_out=metrics.tokens_out - state.metrics.tokens_out,
            tool_calls=metrics.tool_calls - state.metrics.tool_calls,
            keys=sorted(update),
        )
        return {**update, "metrics": metrics}

    node.__name__ = name
    return node


def after_diagnose(state: IncidentState) -> str:
    return "report" if state.escalation_reason or state.diagnosis is None else "propose_remediation"


def after_propose(state: IncidentState) -> str:
    return "human_approval" if state.proposal and state.proposal.kind == "action" else "report"


def after_approval(state: IncidentState) -> str:
    approved = state.approval is not None and state.approval.decision in ("approve", "edit")
    return "execute" if approved else "report"


def build_graph(
    deps: AgentDeps, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> CompiledStateGraph[Any]:
    graph = StateGraph(IncidentState)
    for name, fn in NODES:
        # cast: LangGraph's node protocols are overloads mypy cannot match to an async closure.
        graph.add_node(name, cast(Any, _wrap(name, fn, deps)))
    graph.add_edge(START, "ingest_alert")
    graph.add_edge("ingest_alert", "triage")
    graph.add_edge("triage", "retrieve")
    graph.add_edge("retrieve", "investigate")
    graph.add_edge("investigate", "diagnose")
    graph.add_conditional_edges("diagnose", after_diagnose, ["report", "propose_remediation"])
    graph.add_conditional_edges("propose_remediation", after_propose, ["human_approval", "report"])
    graph.add_conditional_edges("human_approval", after_approval, ["execute", "report"])
    graph.add_edge("execute", "verify")
    graph.add_edge("verify", "report")
    graph.add_edge("report", END)
    return graph.compile(checkpointer=checkpointer)


# Every model type stored in IncidentState, allowed explicitly for checkpoint restore
# (LangGraph will block unregistered types in a future version).
CHECKPOINT_TYPES = [
    ("opspilot.models.alert", "Alert"),
    ("opspilot.models.categories", "RootCauseCategory"),
    ("opspilot.rag.models", "RetrievedChunk"),
    ("opspilot.agent.state", "SecurityFlag"),
    *[
        ("opspilot.models.incident", name)
        for name in (
            "Triage",
            "Evidence",
            "Alternative",
            "Diagnosis",
            "RemediationProposal",
            "ApprovalDecision",
            "ActionResult",
            "Verification",
            "IncidentReport",
            "RunMetrics",
        )
    ],
]


@contextlib.asynccontextmanager
async def sqlite_checkpointer(path: Path) -> AsyncIterator[BaseCheckpointSaver[Any]]:
    """The run checkpointer: every step is saved, so a paused run survives a restart."""
    import aiosqlite
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path.parent.mkdir(parents=True, exist_ok=True)
    serde = JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)
    async with aiosqlite.connect(str(path)) as conn:
        yield AsyncSqliteSaver(conn, serde=serde)


def new_incident_id() -> str:
    return "inc-" + dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


def thread(incident_id: str) -> RunnableConfig:
    """Checkpointer config: one thread per incident."""
    return RunnableConfig(configurable={"thread_id": incident_id})


async def pending_approval(
    graph: CompiledStateGraph[Any], incident_id: str
) -> dict[str, Any] | None:
    """The approval request the run is paused on, if any."""
    snapshot = await graph.aget_state(thread(incident_id))
    for interrupt in snapshot.interrupts:
        value = interrupt.value
        if isinstance(value, dict):
            return value
    return None


async def current_state(graph: CompiledStateGraph[Any], incident_id: str) -> IncidentState | None:
    snapshot = await graph.aget_state(thread(incident_id))
    if not snapshot.values:
        return None
    return IncidentState.model_validate(snapshot.values)


async def start(
    graph: CompiledStateGraph[Any],
    incident_id: str,
    raw_alert: Any,
    *,
    mode: str,
    use_rag: bool,
    clock: Callable[[], float] = time.time,
) -> None:
    """Run from the alert until the end or until a human decision is needed."""
    initial = IncidentState(
        incident_id=incident_id,
        mode=mode,
        use_rag=use_rag,
        raw_alert=raw_alert,
        started_at=clock(),
    )
    await graph.ainvoke(initial, thread(incident_id))


async def resume(
    graph: CompiledStateGraph[Any], incident_id: str, decision: ApprovalDecision
) -> None:
    """Continue a paused run with a human decision (works in a new process too).

    Waiting for a human is not agent time: build the resuming graph with
    ``AgentDeps(budget_from=time.time())`` so the remaining budget counts from the decision.
    """
    await graph.ainvoke(Command(resume=decision.model_dump(mode="json")), thread(incident_id))
