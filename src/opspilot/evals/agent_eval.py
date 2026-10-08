"""Agent evaluation: replay every case under a retrieval configuration and score it.

Runs are sequential (one model in RAM), replayed from recorded fixtures, and approved by
the harness ("simulate-approve"): in replay mode nothing can be executed, so approval only
lets the run reach its report. Each outcome is cached under a key made of the case, the
configuration, the prompt versions, a hash of the agent code and the fixture, so an
interrupted evaluation resumes where it stopped and a code change invalidates the cache.
"""

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import time
from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from opspilot.agent import graph as g
from opspilot.agent.deps import AgentDeps, Budgets, EventSink, NullEvents
from opspilot.agent.observability import RunLog, run_status
from opspilot.agent.prompts import PROMPTS_DIR, load_prompt
from opspilot.agent.state import IncidentState
from opspilot.agent.toolbox import InProcessToolBox, ToolBox
from opspilot.config import Settings
from opspilot.evals.scoring import EvalCase, EvalRow, RunOutcome, inline_refs, score
from opspilot.llm import LLMFactory, LLMPool
from opspilot.logging import get_logger
from opspilot.models.incident import ApprovalDecision

log = get_logger(__name__)

APPROVER = "eval-harness"
# Hard wall-clock limit for one replayed run: the agent's own budget plus slack.
RUN_LIMIT_S = Budgets().run_s + 120.0
HEALTHY_ALERT = Path("evals/agent/alerts/healthy-false-alarm.json")
# Code whose change can change an agent run (scoring is re-applied from cached outcomes).
CODE_PATHS = (
    "src/opspilot/agent",
    "src/opspilot/prompts",
    "src/opspilot/llm.py",
    "src/opspilot/rag",
)


class EvalConfig(BaseModel):
    """One retrieval configuration of the ablation."""

    name: str
    label: str
    use_rag: bool = True
    store: Literal["weaviate", "chroma"] = "weaviate"
    mode: Literal["dense", "keyword", "hybrid"] = "hybrid"
    rerank: bool = False


CONFIGS: dict[str, EvalConfig] = {
    "C0": EvalConfig(name="C0", label="no RAG (tools only)", use_rag=False),
    "C1": EvalConfig(name="C1", label="dense, Chroma", store="chroma", mode="dense"),
    "C2": EvalConfig(name="C2", label="hybrid, Weaviate"),
    "C3": EvalConfig(name="C3", label="hybrid + rerank, Weaviate", rerank=True),
    "C4": EvalConfig(name="C4", label="hybrid + rerank, Chroma", store="chroma", rerank=True),
}
DEFAULT_CONFIG = "C3"


def settings_for(config: EvalConfig, base: Settings) -> Settings:
    """The agent settings for one configuration."""
    return base.model_copy(
        update={
            "default_store": config.store,
            "retrieval_mode": config.mode,
            "agent_rerank": config.rerank,
        }
    )


# ---- outcome extraction ----------------------------------------------------------------


def outcome_from_state(state: IncidentState, latency_s: float) -> RunOutcome:
    """What the agent did, read from its final state."""
    diagnosis = state.diagnosis
    by_cite = {c.citation_id: c.doc_id for c in state.retrieved}
    cited: list[str] = []
    cited_docs: list[str] = []
    if diagnosis is not None:
        cited = list(
            dict.fromkeys(
                diagnosis.evidence_refs + diagnosis.runbook_refs + inline_refs(diagnosis.summary)
            )
        )
        cited_docs = list(dict.fromkeys(by_cite[r] for r in cited if r in by_cite))
    proposal = state.proposal
    result = state.action_result
    metrics = state.metrics
    return RunOutcome(
        use_rag=state.use_rag,
        category=diagnosis.root_cause_category.value if diagnosis else None,
        component=diagnosis.component if diagnosis else None,
        confidence=diagnosis.confidence if diagnosis else None,
        escalated=state.needs_human,
        summary=diagnosis.summary if diagnosis else "",
        retrieved_doc_ids=list(dict.fromkeys(c.doc_id for c in state.retrieved)),
        cited_doc_ids=cited_docs,
        cited_ids=cited,
        valid_ids=sorted(state.evidence_ids() | state.citation_ids()),
        proposal_kind=proposal.kind if proposal else None,
        action=proposal.action if proposal else None,
        approval_decision=state.approval.decision if state.approval else None,
        executed=bool(result and (result.executed or result.simulated)),
        flag_sources=[f.source for f in state.security_flags],
        tool_calls=metrics.tool_calls,
        latency_s=latency_s,
        tokens_in=metrics.tokens_in,
        tokens_out=metrics.tokens_out,
        errors=list(state.errors),
    )


# ---- cache keys ---------------------------------------------------------------------------


def code_hash(root: Path, paths: Iterable[str] = CODE_PATHS) -> str:
    """SHA-256 over the agent's code and prompts (file paths and contents)."""
    digest = hashlib.sha256()
    for rel in paths:
        target = root / rel
        files = sorted(target.rglob("*")) if target.is_dir() else [target]
        for path in files:
            if path.is_file() and path.suffix in (".py", ".md"):
                digest.update(str(path.relative_to(root)).encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def prompt_versions() -> dict[str, str]:
    """Every prompt's version label, e.g. {"diagnose": "diagnose-v1"}."""
    return {p.stem: load_prompt(p.stem).version for p in sorted(PROMPTS_DIR.glob("*.md"))}


def cache_key(case_id: str, config: EvalConfig, *, code: str, fixture_sha: str, model: str) -> str:
    payload = json.dumps(
        {
            "case": case_id,
            "config": config.model_dump(),
            "prompts": prompt_versions(),
            "code": code,
            "fixture": fixture_sha,
            "model": model,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


class OutcomeCache:
    """One JSON file per cache key under ``directory``."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def get(self, key: str) -> tuple[RunOutcome, str] | None:
        path = self.directory / f"{key}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return RunOutcome.model_validate(data["outcome"]), str(data["run_id"])

    def put(self, key: str, outcome: RunOutcome, run_id: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        body = {"outcome": outcome.model_dump(), "run_id": run_id}
        (self.directory / f"{key}.json").write_text(json.dumps(body) + "\n")


# ---- one replayed run --------------------------------------------------------------------

ToolBoxFactory = Callable[
    [Path, EvalConfig, Settings], contextlib.AbstractAsyncContextManager[ToolBox]
]


@contextlib.asynccontextmanager
async def replay_toolbox(
    fixture: Path, config: EvalConfig, settings: Settings
) -> AsyncIterator[ToolBox]:
    """The real k8s (replay) and knowledge-base MCP servers, in process."""
    from opspilot.mcp_servers.common import AuditLog, ToolRunner
    from opspilot.mcp_servers.k8s_readonly.replay import ReplayBackend
    from opspilot.mcp_servers.k8s_readonly.server import K8sTools
    from opspilot.mcp_servers.k8s_readonly.server import build_server as build_k8s
    from opspilot.mcp_servers.knowledge.server import KbTools
    from opspilot.mcp_servers.knowledge.server import build_server as build_kb
    from opspilot.rag.pipeline import build_retriever

    runner = ToolRunner(AuditLog(settings.audit_log, "opspilot-eval", "replay"))
    k8s = K8sTools(ReplayBackend(fixture), runner, settings.allowed_namespaces)
    kb = KbTools(
        lambda: build_retriever(config.store, rerank=False, settings=settings),
        settings.knowledge_dir,
        runner,
    )
    async with InProcessToolBox(build_k8s(k8s), build_kb(kb)) as toolbox:
        yield toolbox


async def run_case(
    alert: Any,
    fixture: Path,
    config: EvalConfig,
    settings: Settings,
    *,
    run_id: str,
    llm: LLMFactory | None = None,
    toolbox_factory: ToolBoxFactory = replay_toolbox,
    events: EventSink | None = None,
) -> RunOutcome:
    """Replay one case end to end with simulated approval; return its outcome."""
    from opspilot.rag.pipeline import build_retriever

    def retriever() -> Any:
        return build_retriever(config.store, rerank=config.rerank, settings=settings)

    pool = LLMPool(settings)
    started = time.time()
    async with (
        toolbox_factory(fixture, config, settings) as toolbox,
        g.sqlite_checkpointer(settings.data_dir / "eval-checkpoints.sqlite") as saver,
    ):
        deps = AgentDeps(
            toolbox=toolbox,
            llm=llm or pool,
            retriever=retriever if config.use_rag else None,
            settings=settings,
            events=events or NullEvents(),
            report_dir=settings.runs_dir / "eval",
        )
        graph = g.build_graph(deps, saver)
        try:
            await g.start(graph, run_id, alert, mode="replay", use_rag=config.use_rag)
            if await g.pending_approval(graph, run_id) is not None:
                decision = ApprovalDecision(
                    decision="approve",
                    approver=APPROVER,
                    reason="simulate-approve: replay mode cannot execute anything",
                )
                await g.resume(graph, run_id, decision)
            state = await g.current_state(graph, run_id)
        finally:
            await pool.aclose()
    assert state is not None
    if isinstance(events, RunLog):  # keep the final state next to the events for analysis
        events.write_state(state, run_status(state, paused=False))
    return outcome_from_state(state, time.time() - started)


# ---- evaluation loop ------------------------------------------------------------------------


def case_alert(case: EvalCase, scenarios_dir: Path) -> Any:
    """The alert a case starts from (the healthy control uses a false-alarm alert)."""
    if case.control:
        return json.loads(HEALTHY_ALERT.read_text())
    from opspilot.faults.scenario import load_scenario

    return load_scenario(scenarios_dir / f"{case.id}.yaml").alert.model_dump()


RunFn = Callable[[EvalCase, EvalConfig, str], Any]


class AgentEval:
    """Runs cases under configurations with caching; writes one JSONL file per config."""

    def __init__(
        self,
        settings: Settings,
        root: Path,
        results_dir: Path,
        *,
        resume: bool,
        run: RunFn | None = None,
        today: str | None = None,
    ) -> None:
        self.settings = settings
        self.root = root
        self.results_dir = results_dir
        self.resume = resume
        self.cache = OutcomeCache(settings.data_dir / "eval-cache")
        self.code = code_hash(root)
        self.today = today or dt.date.today().isoformat()
        self._run = run or self._replay

    def _replay(self, case: EvalCase, config: EvalConfig, run_id: str) -> RunOutcome:
        fixture = self.settings.fixtures_dir / f"{case.id}.json"
        settings = settings_for(config, self.settings)
        events = RunLog(self.settings.runs_dir / "eval", run_id)
        alert = case_alert(case, self.settings.scenarios_dir)
        return asyncio.run(
            asyncio.wait_for(
                run_case(alert, fixture, config, settings, run_id=run_id, events=events),
                RUN_LIMIT_S,
            )
        )

    def key(self, case: EvalCase, config: EvalConfig) -> str:
        fixture = self.settings.fixtures_dir / f"{case.id}.json"
        fixture_sha = hashlib.sha256(fixture.read_bytes()).hexdigest()
        return cache_key(
            case.id, config, code=self.code, fixture_sha=fixture_sha, model=self.settings.llm_model
        )

    def output_path(self, config: EvalConfig) -> Path:
        return self.results_dir / f"agent-{self.today}-{config.name}.jsonl"

    def evaluate(
        self,
        cases: list[EvalCase],
        config: EvalConfig,
        on_row: Callable[[EvalRow, bool], None] | None = None,
    ) -> list[EvalRow]:
        """Run (or reuse) every case for one config; rewrite the JSONL after each case."""
        rows: list[EvalRow] = []
        out = self.output_path(config)
        out.parent.mkdir(parents=True, exist_ok=True)
        versions = prompt_versions()
        for case in cases:
            key = self.key(case, config)
            cached = self.cache.get(key) if self.resume else None
            if cached:
                outcome, run_id = cached
            else:
                run_id = f"eval-{dt.datetime.now(dt.UTC):%Y%m%d-%H%M%S}-{config.name}-{case.id}"
                outcome = self._run(case, config, run_id)
                self.cache.put(key, outcome, run_id)
            row = score(
                case,
                outcome,
                config.name,
                prompt_versions=versions,
                cache_key=key,
                run_id=run_id,
                model=self.settings.llm_model,
            )
            rows.append(row)
            out.write_text("".join(r.model_dump_json() + "\n" for r in rows))
            if on_row:
                on_row(row, cached is not None)
        return rows


def read_rows(path: Path) -> list[EvalRow]:
    """Rows of one results file."""
    return [EvalRow.model_validate_json(line) for line in path.read_text().splitlines() if line]
