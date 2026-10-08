"""``opspilot investigate``, ``opspilot runs list|show`` and ``opspilot resume``."""

import asyncio
import contextlib
import json
import os
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from rich.console import Console
from rich.table import Table

from opspilot.agent import graph as g
from opspilot.agent.deps import AgentDeps, Budgets
from opspilot.agent.observability import RunLog, list_runs, run_status, setup_tracing
from opspilot.agent.state import IncidentState
from opspilot.agent.toolbox import MCPToolBox, ToolBox
from opspilot.config import get_settings
from opspilot.faults.scenario import load_scenarios
from opspilot.llm import LLMFactory, LLMPool
from opspilot.models.incident import ApprovalDecision

console = Console()
runs_app = typer.Typer(help="List and inspect agent runs.")

Mode = Literal["replay", "live"]
Decision = Literal["ask", "approve", "reject"]
ToolBoxFactory = Callable[..., contextlib.AbstractAsyncContextManager[ToolBox]]


class ConsoleEvents:
    """Prints node-by-node progress."""

    def emit(self, kind: str, **data: Any) -> None:
        if kind == "node_start":
            console.print(f"[cyan]→ {data['node']}[/cyan]")
        elif kind == "node_end":
            extra = f", {data['tool_calls']} tool calls" if data.get("tool_calls") else ""
            console.print(f"  [green]✓[/green] {data['node']} {data['ms'] / 1000:.1f}s{extra}")
        elif kind == "tool_call":
            args = ", ".join(f"{k}={v}" for k, v in data["args"].items() if v not in (None, False))
            console.print(f"    · {data['tool']}({args})")
        elif kind == "tool_blocked":
            console.print(f"    [yellow]· blocked repeat: {data['tool']}[/yellow]")


class FanOut:
    def __init__(self, *sinks: Any) -> None:
        self.sinks = sinks

    def emit(self, kind: str, **data: Any) -> None:
        for sink in self.sinks:
            sink.emit(kind, **data)


def mcp_toolbox(
    mode: Mode, fixture: Path | None, run_id: str
) -> contextlib.AbstractAsyncContextManager[ToolBox]:
    """Production tools: MCP stdio sessions (actions included only for live runs)."""
    return MCPToolBox(mode=mode, fixture=fixture, include_actions=mode == "live", run_id=run_id)


# Injection points (tests use in-process servers and a scripted model).
toolbox_factory: ToolBoxFactory = mcp_toolbox
llm_factory: LLMFactory | None = None


def load_alert(scenario: str | None, alert_file: Path | None) -> tuple[Any, Path | None]:
    """The alert to investigate and the default replay fixture for it."""
    settings = get_settings()
    if scenario:
        scenarios = load_scenarios(settings.scenarios_dir)
        if scenario not in scenarios:
            raise typer.BadParameter(f"unknown scenario {scenario!r}")
        return scenarios[scenario].alert.model_dump(), settings.fixtures_dir / f"{scenario}.json"
    if alert_file:
        text = alert_file.read_text()
        try:
            return json.loads(text), None
        except json.JSONDecodeError:
            return text, None
    raise typer.BadParameter("give --scenario or --alert-file")


def show_proposal(pending: dict[str, Any]) -> None:
    diagnosis = pending.get("diagnosis") or {}
    proposal = pending["proposal"]
    console.rule("[bold]Approval needed")
    console.print(
        f"Diagnosis: [bold]{diagnosis.get('root_cause_category')}[/bold] in "
        f"{diagnosis.get('component')} (confidence {diagnosis.get('confidence', 0):.2f})"
    )
    console.print(diagnosis.get("summary", ""))
    console.print(f"\nProposed action: [bold]{json.dumps(proposal['action'])}[/bold]")
    console.print(f"Why: {proposal['rationale']}")
    plan = proposal.get("plan", {})
    for change in plan.get("diff", []):
        console.print(f"  diff {change['path']}: {change['before']} -> {change['after']}")
    if not plan.get("diff"):
        console.print(f"  dry run: {plan.get('dry_run')}")
    for note in plan.get("risk_notes", []):
        console.print(f"  risk: {note}")
    console.print(f"Rollback plan: {proposal.get('rollback_plan')}")
    for flag in pending.get("security_flags", []):
        console.print(
            f"[red]security flag[/red] {flag['pattern']} in {flag['source']}: {flag['excerpt']!r}"
        )


def ask_decision(approver: str | None) -> ApprovalDecision:
    choice = typer.prompt("approve / reject / edit", default="reject").strip().lower()
    who = approver or typer.prompt("approver name")
    reason = typer.prompt("reason", default="")
    if choice == "edit":
        edited = json.loads(
            typer.prompt('fields to change, as JSON (e.g. {"memory_limit": "384Mi"})')
        )
        return ApprovalDecision(decision="edit", approver=who, reason=reason, edited_action=edited)
    decision = "approve" if choice == "approve" else "reject"
    return ApprovalDecision(decision=decision, approver=who, reason=reason)


@contextlib.asynccontextmanager
async def session(
    incident_id: str,
    mode: Mode,
    fixture: Path | None,
    store: str | None,
    use_rag: bool,
    budget_from: float | None = None,
) -> AsyncIterator[tuple[Any, AgentDeps, RunLog]]:
    settings = get_settings()
    if store:
        os.environ["OPSPILOT_DEFAULT_STORE"] = store  # also seen by the kb server subprocess
    run_log = RunLog(settings.runs_dir, incident_id)
    async with (
        toolbox_factory(mode, fixture, incident_id) as toolbox,
        g.sqlite_checkpointer(settings.data_dir / "checkpoints.sqlite") as saver,
    ):

        def retriever() -> Any:
            from opspilot.rag.pipeline import build_retriever

            return build_retriever(store, rerank=settings.agent_rerank)  # type: ignore[arg-type]

        pool = LLMPool(settings)
        deps = AgentDeps(
            toolbox=toolbox,
            llm=llm_factory or pool,
            retriever=retriever if use_rag else None,
            settings=settings,
            events=FanOut(run_log, ConsoleEvents()),
            budgets=Budgets(),
            actions=toolbox if mode == "live" else None,
            report_dir=settings.runs_dir,
            budget_from=budget_from,
        )
        try:
            yield g.build_graph(deps, saver), deps, run_log
        finally:
            await pool.aclose()


async def _finish(
    graph: Any,
    incident_id: str,
    run_log: RunLog,
    decision: Decision,
    approver: str | None,
    interactive: bool,
) -> IncidentState:
    """After a pause: show the proposal, get a decision, resume. Returns the latest state."""
    pending = await g.pending_approval(graph, incident_id)
    state = await g.current_state(graph, incident_id)
    assert state is not None
    run_log.write_state(state, run_status(state, paused=pending is not None))
    if pending is None:
        return state
    show_proposal(pending)
    if decision == "ask" and not interactive:
        console.print(
            f"[yellow]Paused for approval.[/yellow] Resume with: opspilot resume {incident_id}"
        )
        return state
    choice = (
        ask_decision(approver)
        if decision == "ask"
        else ApprovalDecision(
            decision=decision,
            approver=approver or "cli",
            reason="decision given on the command line",
        )
    )
    await g.resume(graph, incident_id, choice)
    state = await g.current_state(graph, incident_id)
    assert state is not None
    run_log.write_state(state, run_status(state, paused=False))
    return state


def _print_outcome(state: IncidentState) -> None:
    console.rule("[bold]Outcome")
    if state.diagnosis:
        d = state.diagnosis
        console.print(
            f"Root cause: [bold]{d.root_cause_category.value}[/bold] in {d.component} "
            f"(confidence {d.confidence:.2f}), cites {', '.join(d.evidence_refs + d.runbook_refs)}"
        )
    if state.escalation_reason:
        console.print(f"[yellow]Needs human:[/yellow] {state.escalation_reason}")
    if state.security_flags:
        console.print(
            f"[red]{len(state.security_flags)} security flag(s)[/red] (flagged, not acted on)"
        )
    m = state.metrics
    console.print(
        f"{m.llm_calls} LLM calls, {m.tokens_in}/{m.tokens_out} tokens, {m.tool_calls} tool calls, "
        f"{sum(m.node_latency_ms.values()) / 1000:.0f}s"
    )
    if state.report:
        console.print(f"Report: [bold]{state.report.path}[/bold]")


def investigate(
    scenario: Annotated[
        str | None, typer.Option(help="Scenario id (its alert, and its fixture in replay).")
    ] = None,
    alert_file: Annotated[
        Path | None, typer.Option(help="Alert JSON (Alertmanager or OpsPilot) or text.")
    ] = None,
    mode: Annotated[
        Mode, typer.Option(help="replay (recorded fixture) or live (cluster).")
    ] = "replay",
    fixture: Annotated[
        Path | None, typer.Option(help="Replay fixture (default: the scenario's).")
    ] = None,
    no_rag: Annotated[bool, typer.Option("--no-rag", help="Skip retrieval (ablation).")] = False,
    store: Annotated[str | None, typer.Option(help="weaviate or chroma for retrieval.")] = None,
    decision: Annotated[
        Decision, typer.Option(help="ask (default), or approve/reject in replay only.")
    ] = "ask",
    approver: Annotated[str | None, typer.Option(help="Name recorded with the decision.")] = None,
) -> None:
    """Investigate an alert end to end, pausing for human approval before any change."""
    if mode == "live" and decision != "ask":
        raise typer.BadParameter(
            "live runs need a decision made after seeing the proposal; use --decision ask"
        )
    alert, default_fixture = load_alert(scenario, alert_file)
    fixture = fixture or default_fixture
    if mode == "replay" and (fixture is None or not fixture.exists()):
        raise typer.BadParameter("replay needs a recorded fixture (--fixture)")
    settings = get_settings()
    setup_tracing(settings)
    incident_id = g.new_incident_id()
    run_dir = settings.runs_dir / incident_id
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "mode": mode,
        "fixture": str(fixture) if mode == "replay" else None,
        "store": store,
        "use_rag": not no_rag,
        "scenario": scenario,
    }
    (run_dir / "run.json").write_text(json.dumps(meta, indent=1) + "\n")
    console.print(f"Incident [bold]{incident_id}[/bold] ({mode})")

    async def go() -> IncidentState:
        async with session(
            incident_id, mode, fixture if mode == "replay" else None, store, not no_rag
        ) as (
            graph,
            deps,
            run_log,
        ):
            await asyncio.wait_for(
                g.start(graph, incident_id, alert, mode=mode, use_rag=not no_rag),
                deps.budgets.run_s + 60,
            )
            return await _finish(
                graph, incident_id, run_log, decision, approver, sys.stdin.isatty()
            )

    _print_outcome(asyncio.run(go()))


def resume(
    incident_id: Annotated[str, typer.Argument(help="Incident (thread) id of a paused run.")],
    decision: Annotated[Decision, typer.Option(help="ask, approve or reject.")] = "ask",
    approver: Annotated[str | None, typer.Option(help="Name recorded with the decision.")] = None,
) -> None:
    """Resume a run paused for approval, from any process (state is checkpointed)."""
    settings = get_settings()
    meta_path = settings.runs_dir / incident_id / "run.json"
    if not meta_path.exists():
        raise typer.BadParameter(f"no run {incident_id}")
    meta = json.loads(meta_path.read_text())
    fixture = Path(meta["fixture"]) if meta.get("fixture") else None

    async def go() -> IncidentState:
        async with session(
            incident_id,
            meta["mode"],
            fixture,
            meta.get("store"),
            meta["use_rag"],
            budget_from=time.time(),
        ) as (graph, _, run_log):
            return await _finish(
                graph, incident_id, run_log, decision, approver, sys.stdin.isatty()
            )

    _print_outcome(asyncio.run(go()))


@runs_app.command("list")
def runs_list() -> None:
    """Every recorded run, newest first."""
    table = Table("incident", "status", "mode", "category", "conf", "action", "alert")
    for r in list_runs(get_settings().runs_dir):
        conf = f"{r.confidence:.2f}" if r.confidence is not None else "-"
        table.add_row(
            r.incident_id, r.status, r.mode, r.category or "-", conf, r.action or "-", r.alert
        )
    console.print(table)


@runs_app.command("show")
def runs_show(incident_id: Annotated[str, typer.Argument(help="Incident id.")]) -> None:
    """Print a run's report (or its state if it has no report yet) and event counts."""
    run_dir = get_settings().runs_dir / incident_id
    if not run_dir.exists():
        raise typer.BadParameter(f"no run {incident_id}")
    report = run_dir / "report.md"
    console.print(report.read_text() if report.exists() else (run_dir / "state.json").read_text())
    events = RunLog(get_settings().runs_dir, incident_id).events()
    counts: dict[str, int] = {}
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    console.print(f"[dim]events: {counts}[/dim]")
