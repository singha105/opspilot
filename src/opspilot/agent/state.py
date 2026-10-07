"""The LangGraph state of one incident run."""

import operator
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from opspilot.models import Alert
from opspilot.models.incident import (
    ActionResult,
    ApprovalDecision,
    Diagnosis,
    Evidence,
    IncidentReport,
    RemediationProposal,
    RunMetrics,
    Triage,
    Verification,
)
from opspilot.rag.models import RetrievedChunk

Mode = Literal["live", "replay"]


class SecurityFlag(BaseModel):
    """Suspicious content found in untrusted data. Flagged and reported, never acted on."""

    source: str
    pattern: str
    excerpt: str = Field(max_length=200)


class IncidentState(BaseModel):
    """Everything the graph knows about one incident. Lists are append-only."""

    incident_id: str
    mode: Mode = "replay"
    use_rag: bool = True
    raw_alert: Any = None
    started_at: float = 0.0

    alert: Alert | None = None
    triage: Triage | None = None
    retrieved: list[RetrievedChunk] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)
    evidence: Annotated[list[Evidence], operator.add] = Field(default_factory=list)
    diagnosis: Diagnosis | None = None
    escalation_reason: str | None = None
    proposal: RemediationProposal | None = None
    approval: ApprovalDecision | None = None
    action_result: ActionResult | None = None
    verification: Verification | None = None
    report: IncidentReport | None = None

    security_flags: Annotated[list[SecurityFlag], operator.add] = Field(default_factory=list)
    errors: Annotated[list[str], operator.add] = Field(default_factory=list)
    metrics: RunMetrics = Field(default_factory=RunMetrics)

    @property
    def needs_human(self) -> bool:
        return self.escalation_reason is not None

    def evidence_ids(self) -> set[str]:
        return {e.id for e in self.evidence}

    def citation_ids(self) -> set[str]:
        return {c.citation_id for c in self.retrieved}
