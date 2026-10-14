"""Incident service: the one place that starts, pauses, resumes and lists agent runs.

The CLI, the HTTP API and the operator console all go through ``IncidentService``, so the
rules live here once: a replay needs a recorded fixture, a live run is never approved in
advance, one agent run executes at a time (one local model in RAM), and every state change
is written to ``runs/<incident>/`` where any process can read it.
"""

import asyncio
import contextlib
import json
import os
import time
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from opspilot.agent import graph as g
from opspilot.agent.deps import AgentDeps, Budgets, EventSink
from opspilot.agent.observability import RunLog, RunSummary, list_runs, run_status
from opspilot.agent.state import IncidentState
from opspilot.agent.toolbox import MCPToolBox, ToolBox
from opspilot.config import Settings, get_settings
from opspilot.faults.scenario import load_scenarios
from opspilot.llm import LLMFactory, LLMPool
from opspilot.models.incident import ApprovalDecision

Mode = Literal["replay", "live"]
ToolBoxFactory = Callable[..., contextlib.AbstractAsyncContextManager[ToolBox]]
PENDING = "pending.json"
META = "run.json"


class ServiceError(ValueError):
    """A request the service refuses (bad input or wrong state); safe to show to users."""


class RunMeta(BaseModel):
    """How a run was started (``runs/<incident>/run.json``)."""

    mode: Mode
    fixture: str | None = None
    store: str | None = None
    use_rag: bool = True
    scenario: str | None = None
    source: str = "cli"


class IncidentView(BaseModel):
    """Everything a client needs to show one incident."""

    incident_id: str
    status: str
    meta: RunMeta
    state: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None
    report_markdown: str | None = None


def mcp_toolbox(
    mode: Mode, fixture: Path | None, run_id: str
) -> contextlib.AbstractAsyncContextManager[ToolBox]:
    """Production tools: MCP stdio sessions (actions included only for live runs)."""
    return MCPToolBox(mode=mode, fixture=fixture, include_actions=mode == "live", run_id=run_id)


class _FanOut:
    def __init__(self, sinks: Sequence[EventSink]) -> None:
        self.sinks = sinks

    def emit(self, kind: str, **data: Any) -> None:
        for sink in self.sinks:
            sink.emit(kind, **data)


class IncidentService:
    """Start, resume and inspect incidents; shared by the CLI, the API and the UI."""

    # One agent run at a time per process (one model in RAM); asyncio locks belong to a loop.
    _locks: ClassVar[dict[int, asyncio.Lock]] = {}

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        toolbox_factory: ToolBoxFactory = mcp_toolbox,
        llm_factory: LLMFactory | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.toolbox_factory = toolbox_factory
        self.llm_factory = llm_factory

    # ---- inputs --------------------------------------------------------------------------

    def scenarios(self) -> dict[str, Any]:
        """Scenarios that have a recorded fixture (what replay mode can run)."""
        return {
            sid: s
            for sid, s in load_scenarios(self.settings.scenarios_dir).items()
            if (self.settings.fixtures_dir / f"{sid}.json").exists()
        }

    def scenario_alert(self, scenario: str) -> tuple[dict[str, Any], Path]:
        """A scenario's alert and its replay fixture."""
        scenarios = load_scenarios(self.settings.scenarios_dir)
        if scenario not in scenarios:
            raise ServiceError(f"unknown scenario {scenario!r}")
        alert = scenarios[scenario].alert.model_dump()
        return alert, self.settings.fixtures_dir / f"{scenario}.json"

    # ---- lifecycle -----------------------------------------------------------------------

    def create(
        self,
        *,
        mode: Mode,
        scenario: str | None = None,
        fixture: Path | None = None,
        store: str | None = None,
        use_rag: bool = True,
        source: str = "cli",
    ) -> str:
        """Validate a request and register a new incident; returns its id."""
        if scenario and fixture is None:
            _, fixture = self.scenario_alert(scenario)
        if mode == "replay" and (fixture is None or not fixture.exists()):
            raise ServiceError("replay needs a recorded fixture (a scenario or --fixture)")
        incident_id = g.new_incident_id()
        run_dir = self.settings.runs_dir / incident_id
        run_dir.mkdir(parents=True, exist_ok=True)
        meta = RunMeta(
            mode=mode,
            fixture=str(fixture) if mode == "replay" and fixture else None,
            store=store,
            use_rag=use_rag,
            scenario=scenario,
            source=source,
        )
        (run_dir / META).write_text(meta.model_dump_json(indent=1) + "\n")
        return incident_id

    async def run(
        self, incident_id: str, alert: Any, sinks: Sequence[EventSink] = ()
    ) -> IncidentView:
        """Run a new incident until it needs a decision or ends."""
        meta = self.meta(incident_id)
        async with self._exclusive(), self._session(incident_id, meta, sinks) as (graph, deps):
            await asyncio.wait_for(
                g.start(graph, incident_id, alert, mode=meta.mode, use_rag=meta.use_rag),
                deps.budgets.run_s + 60,
            )
            await self._save(graph, incident_id)
        return self.get(incident_id)

    async def decide(
        self, incident_id: str, decision: ApprovalDecision, sinks: Sequence[EventSink] = ()
    ) -> IncidentView:
        """Resume a paused incident with a human decision (from any process)."""
        if self.pending(incident_id) is None:
            raise ServiceError(f"incident {incident_id} is not waiting for approval")
        meta = self.meta(incident_id)
        async with (
            self._exclusive(),
            self._session(incident_id, meta, sinks, budget_from=time.time()) as (graph, _),
        ):
            await g.resume(graph, incident_id, decision)
            await self._save(graph, incident_id)
        return self.get(incident_id)

    # ---- reads ---------------------------------------------------------------------------

    def meta(self, incident_id: str) -> RunMeta:
        path = self.settings.runs_dir / incident_id / META
        if not path.exists():
            raise ServiceError(f"no incident {incident_id}")
        return RunMeta.model_validate_json(path.read_text())

    def pending(self, incident_id: str) -> dict[str, Any] | None:
        path = self.settings.runs_dir / incident_id / PENDING
        return json.loads(path.read_text()) if path.exists() else None

    def get(self, incident_id: str) -> IncidentView:
        meta = self.meta(incident_id)
        run_dir = self.settings.runs_dir / incident_id
        state_path, report_path = run_dir / "state.json", run_dir / "report.md"
        state = json.loads(state_path.read_text()) if state_path.exists() else None
        return IncidentView(
            incident_id=incident_id,
            status=state.get("status", "running") if state else "running",
            meta=meta,
            state=state,
            pending=self.pending(incident_id),
            report_markdown=report_path.read_text() if report_path.exists() else None,
        )

    def list(self) -> list[RunSummary]:
        return list_runs(self.settings.runs_dir)

    # ---- internals -----------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _exclusive(self) -> AsyncIterator[None]:
        lock = self._locks.setdefault(id(asyncio.get_running_loop()), asyncio.Lock())
        async with lock:
            yield

    @contextlib.asynccontextmanager
    async def _session(
        self,
        incident_id: str,
        meta: RunMeta,
        sinks: Sequence[EventSink],
        budget_from: float | None = None,
    ) -> AsyncIterator[tuple[Any, AgentDeps]]:
        settings = self.settings
        if meta.store:
            os.environ["OPSPILOT_DEFAULT_STORE"] = meta.store  # seen by the kb server too
        run_log = RunLog(settings.runs_dir, incident_id)
        fixture = Path(meta.fixture) if meta.fixture else None
        async with (
            self.toolbox_factory(meta.mode, fixture, incident_id) as toolbox,
            g.sqlite_checkpointer(settings.data_dir / "checkpoints.sqlite") as saver,
        ):

            def retriever() -> Any:
                from opspilot.rag.pipeline import build_retriever

                return build_retriever(meta.store, rerank=settings.agent_rerank)  # type: ignore[arg-type]

            pool = LLMPool(settings)
            deps = AgentDeps(
                toolbox=toolbox,
                llm=self.llm_factory or pool,
                retriever=retriever if meta.use_rag else None,
                settings=settings,
                events=_FanOut([run_log, *sinks]),
                budgets=Budgets(),
                actions=toolbox if meta.mode == "live" else None,
                report_dir=settings.runs_dir,
                budget_from=budget_from,
            )
            try:
                yield g.build_graph(deps, saver), deps
            finally:
                await pool.aclose()

    async def _save(self, graph: Any, incident_id: str) -> None:
        """Write the state, and the approval request while the run is paused."""
        pending = await g.pending_approval(graph, incident_id)
        state = await g.current_state(graph, incident_id)
        assert state is not None
        RunLog(self.settings.runs_dir, incident_id).write_state(
            state, run_status(state, paused=pending is not None)
        )
        path = self.settings.runs_dir / incident_id / PENDING
        if pending is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(pending, indent=1, default=str) + "\n")


def load_state(view: IncidentView) -> IncidentState | None:
    """The typed state of a view (``None`` while the first run is in progress)."""
    if view.state is None:
        return None
    data = {k: v for k, v in view.state.items() if k not in ("status", "updated_at")}
    return IncidentState.model_validate(data)
