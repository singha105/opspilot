"""Models for one incident run: triage, evidence, diagnosis, remediation, approval, report."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from opspilot.models.categories import RootCauseCategory

ActionName = Literal[
    "restart_deployment",
    "rollback_deployment",
    "scale_deployment",
    "patch_container_resources",
    "set_container_image",
]


class Triage(BaseModel):
    """First read of the alert: where to look and what it might be."""

    service: str = Field(description="Affected Kubernetes Deployment, e.g. payments-api.")
    namespace: str = Field(description="Kubernetes namespace of the affected service.")
    symptom_summary: str = Field(description="One or two sentences describing the symptom.")
    candidate_categories: list[RootCauseCategory] = Field(min_length=1, max_length=3)
    search_queries: list[str] = Field(min_length=2, max_length=3)


class Evidence(BaseModel):
    """One tool observation, summarized for the model and kept for the report."""

    id: str  # E1, E2, ...
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    summary: str = Field(max_length=300)
    excerpt: str = Field(default="", max_length=800)
    error: bool = False


class Alternative(BaseModel):
    category: RootCauseCategory
    why_less_likely: str


class Diagnosis(BaseModel):
    root_cause_category: RootCauseCategory
    component: str = Field(description="Deployment that holds the root cause.")
    summary: str = Field(description="Root cause in one or two sentences, citing [E#] and [R#].")
    evidence_refs: list[str] = Field(description="Evidence ids that support the diagnosis.")
    runbook_refs: list[str] = Field(default_factory=list, description="Runbook citation ids.")
    confidence: float = Field(ge=0.0, le=1.0)
    alternatives: list[Alternative] = Field(default_factory=list, max_length=3)


class RemediationProposal(BaseModel):
    """What OpsPilot suggests. ``action`` is set only for allowlisted automated actions."""

    kind: Literal["action", "manual_change", "none"]
    action: dict[str, Any] | None = None
    action_hash: str | None = None
    rationale: str
    plan: dict[str, Any] = Field(default_factory=dict)  # dry-run diff and risk notes
    rollback_plan: str = ""
    manual_change: str | None = None


class ApprovalDecision(BaseModel):
    """A human's answer to a proposal."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject", "edit"]
    approver: str = Field(min_length=1)
    reason: str = ""
    edited_action: dict[str, Any] | None = None


class ActionResult(BaseModel):
    executed: bool
    simulated: bool = False
    action: dict[str, Any] | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class Verification(BaseModel):
    status: Literal["resolved", "not_resolved", "not_applicable", "skipped"]
    details: str
    elapsed_s: float = 0.0


class IncidentReport(BaseModel):
    path: str
    markdown: str
    needs_human: bool


class RunMetrics(BaseModel):
    """Token, tool and latency accounting for one run."""

    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    node_latency_ms: dict[str, float] = Field(default_factory=dict)

    def add_usage(self, usage: dict[str, Any] | None) -> None:
        self.llm_calls += 1
        if usage:
            self.tokens_in += int(usage.get("input_tokens", 0) or 0)
            self.tokens_out += int(usage.get("output_tokens", 0) or 0)
