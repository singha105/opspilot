"""Dependencies shared by every node of one run (injected, so tests can fake them)."""

import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from opspilot.agent.toolbox import ToolBox
from opspilot.config import Settings, get_settings
from opspilot.llm import LLMFactory, get_llm
from opspilot.rag.retriever import Retriever


class EventSink(Protocol):
    """Receives run events (node transitions, tool calls, prompt versions)."""

    def emit(self, kind: str, **data: Any) -> None: ...


class NullEvents:
    def emit(self, kind: str, **data: Any) -> None:
        return None


@dataclass
class Budgets:
    """Hard limits for one run; the run ends cleanly when any is reached."""

    tool_calls: int = 8
    max_repairs: int = 2
    llm_timeout_s: float = 150.0
    run_s: float = 360.0
    verify_s: float = 90.0
    verify_interval_s: float = 5.0


@dataclass
class AgentDeps:
    toolbox: ToolBox
    llm: LLMFactory = get_llm
    retriever: Callable[[], Retriever] | None = None
    settings: Settings = field(default_factory=get_settings)
    events: EventSink = field(default_factory=NullEvents)
    budgets: Budgets = field(default_factory=Budgets)
    # "Now" for judging how recent a rollout is: the wall clock live, recording time in replay.
    reference_time: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)
    actions: ToolBox | None = None  # the gated actions server; only set for live runs
    report_dir: Path = Path("runs")
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Any] | None = None

    def remaining(self, started_at: float) -> float:
        return self.budgets.run_s - (self.clock() - started_at)

    def timeout(self, started_at: float) -> float:
        """Per-call timeout: the node budget, but never past the end of the run."""
        return max(min(self.budgets.llm_timeout_s, self.remaining(started_at)), 1.0)
