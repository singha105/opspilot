"""Guards that hold regardless of what the model says.

- Citations: every E-id and R-id must exist.
- Injection: instructions hidden in untrusted data are flagged, never acted on.
- Allowlist: only the actions mapped to the diagnosed category can be proposed.
- Escalation: low confidence or UNKNOWN goes to a human with no action.
"""

import ipaddress
import re
from urllib.parse import urlparse

from opspilot.agent.evidence import pods_from_evidence
from opspilot.agent.state import SecurityFlag
from opspilot.models import ActionName, RootCauseCategory
from opspilot.models.incident import Diagnosis, Evidence

CONFIDENCE_THRESHOLD = 0.5
_INLINE_REF = re.compile(r"\[([ER]\d+)\]")

# ---- citations --------------------------------------------------------------------------


def normalize_refs(diagnosis: Diagnosis) -> Diagnosis:
    """Put E-ids in evidence_refs and R-ids in runbook_refs (small models mix the fields)."""
    refs = list(dict.fromkeys(diagnosis.evidence_refs + diagnosis.runbook_refs))
    evidence = [r for r in refs if not r.startswith("R")]
    runbooks = [r for r in refs if r.startswith("R")]
    if evidence == diagnosis.evidence_refs and runbooks == diagnosis.runbook_refs:
        return diagnosis
    return diagnosis.model_copy(update={"evidence_refs": evidence, "runbook_refs": runbooks})


def citation_problems(
    diagnosis: Diagnosis, evidence_ids: set[str], citation_ids: set[str]
) -> list[str]:
    """Problems with the diagnosis's references (empty list = valid)."""
    problems = []
    if not diagnosis.evidence_refs:
        problems.append("evidence_refs is empty: cite at least one E-id")
    unknown_e = [r for r in diagnosis.evidence_refs if r not in evidence_ids]
    unknown_r = [r for r in diagnosis.runbook_refs if r not in citation_ids]
    inline = set(_INLINE_REF.findall(diagnosis.summary))
    unknown_inline = sorted(r for r in inline if r not in evidence_ids | citation_ids)
    if unknown_e:
        problems.append(f"unknown evidence ids {unknown_e}; valid: {sorted(evidence_ids)}")
    if unknown_r:
        problems.append(f"unknown runbook ids {unknown_r}; valid: {sorted(citation_ids)}")
    if unknown_inline:
        problems.append(f"summary cites unknown ids {unknown_inline}")
    return problems


# ---- prompt-injection detection ---------------------------------------------------------

_AI = r"(?:ai|a\.i\.|assistant|agent|llm|chatbot|language model|copilot|opspilot|bot)"
INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "ignore_previous",
        re.compile(
            r"(?i)\b(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)"
            r"\s+(instructions|prompts|rules|messages)"
        ),
    ),
    (
        "addressed_to_ai",
        re.compile(
            rf"(?i)(\b(dear|attention|hey|hello|note to( the)?|to the)\s+{_AI}\b"
            rf"|\b{_AI}s?\b[^.\n]{{0,40}}\b(must|should|shall|needs? to|is required to)\b)"
        ),
    ),
    (
        "role_override",
        re.compile(
            r"(?i)(you are now|new instructions|system prompt|act as (an?|the) "
            r"|<\|im_start\|>|</?system>|\bdeveloper mode\b)"
        ),
    ),
    (
        "kubectl_write",
        re.compile(
            r"(?i)\bkubectl\s+(-n\s+\S+\s+)?(delete|apply|create|patch|scale|edit|replace|exec"
            r"|drain|cordon|uncordon|label|annotate|set|rollout\s+(undo|restart))\b"
        ),
    ),
    (
        "approval_claim",
        re.compile(
            r"(?i)(\bauto[- ]?approve|\bapprove (this|the|all|every)\b"
            r"|approval (is )?(granted|given)|\bapproval[_ ]token\b"
            r"|you are (authori[sz]ed|approved|permitted))"
        ),
    ),
]
_URL = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)
INTERNAL_SUFFIXES = (".svc", ".svc.cluster.local", ".cluster.local", ".local", ".internal")


def _external_url(url: str) -> bool:
    """True for URLs that leave the cluster (not single-label, *.svc, private IP or localhost)."""
    host = (urlparse(url).hostname or "").lower()
    if not host or host == "localhost" or "." not in host or host.endswith(INTERNAL_SUFFIXES):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (ip.is_private or ip.is_loopback or ip.is_link_local)


# Runbooks and docs legitimately contain kubectl commands and links; in documents only
# the instruction-style patterns count. Tool output (logs, events, config) gets all of them.
DOCUMENT_SKIP = frozenset({"kubectl_write", "external_url"})


def detect_injection(text: str, source: str, *, document: bool = False) -> list[SecurityFlag]:
    """Flag text that tries to instruct the agent. One flag per pattern per text."""
    flags = []
    for name, pattern in INJECTION_PATTERNS:
        if document and name in DOCUMENT_SKIP:
            continue
        match = pattern.search(text)
        if match:
            start = max(match.start() - 60, 0)
            flags.append(
                SecurityFlag(
                    source=source, pattern=name, excerpt=text[start : match.end() + 60][:200]
                )
            )
    external = [] if document else [u for u in _URL.findall(text) if _external_url(u)]
    if external:
        flags.append(SecurityFlag(source=source, pattern="external_url", excerpt=external[0][:200]))
    return flags


# ---- action allowlist ---------------------------------------------------------------------

ALLOWED_ACTIONS: dict[RootCauseCategory, tuple[ActionName, ...]] = {
    RootCauseCategory.OOM_KILLED: ("patch_container_resources", "rollback_deployment"),
    RootCauseCategory.IMAGE_PULL_ERROR: ("set_container_image", "rollback_deployment"),
    RootCauseCategory.BAD_ROLLOUT: ("rollback_deployment",),
    RootCauseCategory.DEPENDENCY_UNAVAILABLE: ("scale_deployment",),
}
# Rollback only when the rollout history shows a recent change; otherwise a manual change.
ROLLBACK_IF_RECENT: frozenset[RootCauseCategory] = frozenset(
    {
        RootCauseCategory.CONFIG_MISSING_ENV,
        RootCauseCategory.MISSING_CONFIGMAP_OR_SECRET,
        RootCauseCategory.LIVENESS_PROBE_MISCONFIG,
        RootCauseCategory.READINESS_PROBE_MISCONFIG,
        RootCauseCategory.BAD_COMMAND,
        RootCauseCategory.INIT_CONTAINER_FAILURE,
    }
)


class GuardViolationError(Exception):
    """A proposal broke a guard (unknown action, action not allowed for the category)."""


def allowed_actions(category: RootCauseCategory, recent_change: bool) -> tuple[ActionName, ...]:
    """Actions that may be proposed for a category; empty means manual change only."""
    if category in ALLOWED_ACTIONS:
        return ALLOWED_ACTIONS[category]
    if category in ROLLBACK_IF_RECENT and recent_change:
        return ("rollback_deployment",)
    return ()


def check_action_allowed(
    category: RootCauseCategory, action_type: str, recent_change: bool
) -> None:
    allowed = allowed_actions(category, recent_change)
    if action_type not in allowed:
        raise GuardViolationError(
            f"action {action_type!r} is not allowed for {category.value}; allowed: {list(allowed)}"
        )


# ---- escalation ---------------------------------------------------------------------------


# Categories whose fault shows on the component's own pods (not ready, or restarting).
# Excluded: faults that leave pods healthy (a Service selector, a bad release that still
# passes its probes) and UNKNOWN.
HEALTHY_PODS_POSSIBLE = {
    RootCauseCategory.SERVICE_MISCONFIG,
    RootCauseCategory.BAD_ROLLOUT,
    RootCauseCategory.UNKNOWN,
}


def pods_contradict(diagnosis: Diagnosis, evidence: list[Evidence]) -> str | None:
    """Why the evidence contradicts the diagnosis: its component's pods are all healthy."""
    if diagnosis.root_cause_category in HEALTHY_PODS_POSSIBLE:
        return None
    pods = [p for p in pods_from_evidence(evidence) if p.name.startswith(f"{diagnosis.component}-")]
    if not pods or any(p.unhealthy or p.restarted for p in pods):
        return None
    cited = ", ".join(sorted({f"[{p.evidence_id}]" for p in pods}))
    return (
        f"{diagnosis.component} has {len(pods)} pod(s), all ready and never restarted {cited}; "
        f"a {diagnosis.root_cause_category.value} fault in {diagnosis.component} would show there"
    )


def escalation_reason(diagnosis: Diagnosis, threshold: float = CONFIDENCE_THRESHOLD) -> str | None:
    """Why this diagnosis must go to a human instead of to remediation (None = proceed)."""
    if diagnosis.root_cause_category == RootCauseCategory.UNKNOWN:
        return "the root cause is UNKNOWN"
    if diagnosis.confidence < threshold:
        return f"confidence {diagnosis.confidence:.2f} is below {threshold:.2f}"
    return None
