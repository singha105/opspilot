"""Per-run records (runs/<incident>/events.jsonl, state.json, report.md) and optional tracing.

JSONL run logs are always written. Phoenix tracing with OpenInference's LangChain
instrumentation is opt-in (``OPSPILOT_TRACING=1``) and needs the ``tracing`` dependency
group plus a Phoenix server (``docker compose --profile tracing up phoenix``).
"""

import datetime as dt
import json
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from opspilot.agent.state import IncidentState
from opspilot.config import Settings
from opspilot.logging import get_logger

log = get_logger(__name__)


class RunLog:
    """Event sink writing one JSON line per event into the run folder."""

    def __init__(self, runs_dir: Path, incident_id: str) -> None:
        self.dir = runs_dir / incident_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self.state_path = self.dir / "state.json"
        self._lock = threading.Lock()

    def emit(self, kind: str, **data: Any) -> None:
        record = {
            "ts": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"),
            "kind": kind,
            **data,
        }
        line = json.dumps(record, default=str, separators=(",", ":"))
        with self._lock, self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def write_state(self, state: IncidentState, status: str) -> None:
        data = state.model_dump(mode="json", exclude={"raw_alert"})
        data["status"] = status
        data["updated_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        self.state_path.write_text(json.dumps(data, indent=1, default=str) + "\n")

    def events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        return [json.loads(line) for line in self.events_path.read_text().splitlines() if line]


class RunSummary(BaseModel):
    incident_id: str
    status: str
    mode: str
    alert: str
    category: str | None
    confidence: float | None
    action: str | None
    updated_at: str


def run_status(state: IncidentState, paused: bool) -> str:
    if paused:
        return "awaiting_approval"
    if state.report is None:
        return "incomplete"
    if state.needs_human:
        return "escalated"
    if state.verification and state.verification.status == "resolved":
        return "resolved"
    return "reported"


def list_runs(runs_dir: Path) -> list[RunSummary]:
    """Summaries of every run folder with a state.json, newest first."""
    summaries = []
    for path in sorted(runs_dir.glob("*/state.json")):
        data = json.loads(path.read_text())
        diagnosis = data.get("diagnosis") or {}
        proposal = data.get("proposal") or {}
        summaries.append(
            RunSummary(
                incident_id=data["incident_id"],
                status=data.get("status", "unknown"),
                mode=data.get("mode", "?"),
                alert=(data.get("alert") or {}).get("summary", "")[:80],
                category=diagnosis.get("root_cause_category"),
                confidence=diagnosis.get("confidence"),
                action=(proposal.get("action") or {}).get("type") or proposal.get("kind"),
                updated_at=data.get("updated_at", ""),
            )
        )
    return sorted(summaries, key=lambda s: s.updated_at, reverse=True)


def setup_tracing(settings: Settings) -> bool:
    """Send LangChain spans to Phoenix if enabled and installed. Returns True if active."""
    if not settings.tracing:
        return False
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from phoenix.otel import register
    except ImportError:
        log.warning("tracing requested but not installed; run: uv sync --group tracing")
        return False
    provider = register(
        project_name="opspilot", endpoint=settings.phoenix_endpoint, verbose=False, batch=True
    )
    LangChainInstrumentor().instrument(tracer_provider=provider)
    return True
