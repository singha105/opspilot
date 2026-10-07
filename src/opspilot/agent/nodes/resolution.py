"""Resolution stage: diagnose, propose, ask a human, execute, verify, report.

Nothing is executed unless ``human_approval`` returned an approve/edit decision from a
human, and the token is minted only after that, in ``execute``.
"""

import asyncio
import datetime as dt
import json
import re
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import interrupt
from pydantic import BaseModel, Field, ValidationError

from opspilot.agent.approval import action_hash, mint_approval_token
from opspilot.agent.deps import AgentDeps
from opspilot.agent.evidence import pods_from_evidence
from opspilot.agent.guards import (
    GuardViolationError,
    allowed_actions,
    check_action_allowed,
    citation_problems,
    escalation_reason,
    normalize_refs,
    pods_contradict,
)
from opspilot.agent.nodes.common import (
    CATEGORIES,
    alert_block,
    chunks_block,
    compact_json,
    evidence_block,
    prompt,
)
from opspilot.agent.state import IncidentState, SecurityFlag
from opspilot.llm import StructuredOutputError, astructured
from opspilot.mcp_servers.actions.models import check_bounds, parse_action
from opspilot.mcp_servers.common.errors import ToolError
from opspilot.models import RootCauseCategory
from opspilot.models.incident import (
    ActionName,
    ActionResult,
    ApprovalDecision,
    Diagnosis,
    IncidentReport,
    RemediationProposal,
    Verification,
)

RECENT_WINDOW = dt.timedelta(hours=2)
_LIMIT = re.compile(r"memory=(\d+)Mi")

# ---- diagnose ----------------------------------------------------------------------------


def _check(diagnosis: Diagnosis, state: IncidentState) -> tuple[list[str], str | None]:
    """Citation problems, and a contradiction between the diagnosis and the pods seen."""
    problems = citation_problems(diagnosis, state.evidence_ids(), state.citation_ids())
    return problems, pods_contradict(diagnosis, state.evidence)


async def diagnose(state: IncidentState, deps: AgentDeps) -> dict[str, Any]:
    assert state.alert is not None
    assert state.triage is not None
    template = prompt("diagnose")
    deps.events.emit("prompt", node="diagnose", version=template.version, sha256=template.sha256)
    text = template.render(
        alert=alert_block(state.alert),
        triage=compact_json(state.triage),
        retrieved_chunks=chunks_block(state.retrieved),
        evidence_items=evidence_block(state.evidence),
        categories=CATEGORIES,
    )
    metrics = state.metrics.model_copy()
    messages: list[Any] = [HumanMessage(text)]
    timeout = deps.timeout(state.started_at)
    try:
        diagnosis = normalize_refs(
            await astructured(
                deps.llm("diagnose"),
                Diagnosis,
                messages,
                metrics,
                max_repairs=deps.budgets.max_repairs,
                timeout_s=timeout,
            )
        )
        problems, conflict = _check(diagnosis, state)
        if problems or conflict:  # one repair round, then escalate
            notes = problems + ([f"The evidence contradicts this: {conflict}."] if conflict else [])
            messages += [
                AIMessage(diagnosis.model_dump_json()),
                HumanMessage(
                    "Fix these problems and answer again. The component is where the fault "
                    "is: a dependency that is down may have no ready pods or endpoints at all. "
                    "If no component shows a fault, answer UNKNOWN. " + " ".join(notes)
                ),
            ]
            diagnosis = normalize_refs(
                await astructured(
                    deps.llm("diagnose"),
                    Diagnosis,
                    messages,
                    metrics,
                    max_repairs=1,
                    timeout_s=timeout,
                )
            )
            problems, conflict = _check(diagnosis, state)
            if problems:
                return {
                    "diagnosis": diagnosis,
                    "escalation_reason": "the diagnosis cites evidence that does not exist: "
                    + "; ".join(problems),
                    "metrics": metrics,
                }
            if conflict:
                pods = pods_from_evidence(state.evidence)
                healthy = bool(pods) and not any(p.unhealthy or p.restarted for p in pods)
                lead = (
                    "no active fault found" if healthy else "the diagnosis contradicts the evidence"
                )
                return {
                    "diagnosis": diagnosis,
                    "escalation_reason": f"{lead}: {conflict}",
                    "metrics": metrics,
                }
    except (StructuredOutputError, TimeoutError) as exc:
        return {
            "escalation_reason": f"no valid diagnosis from the model ({type(exc).__name__})",
            "errors": [f"diagnose: {exc}"],
            "metrics": metrics,
        }
    return {
        "diagnosis": diagnosis,
        "escalation_reason": escalation_reason(diagnosis),
        "metrics": metrics,
    }


# ---- propose_remediation --------------------------------------------------------------------


class RemediationChoice(BaseModel):
    """What the model proposes; code decides whether it is allowed."""

    action_type: ActionName | Literal["manual_change"]
    deployment: str
    container: str = "app"
    memory_limit: str | None = None
    cpu_limit: str | None = None
    replicas: int | None = None
    image: str | None = None
    to_revision: int | None = None
    rationale: str = Field(default="", max_length=400)
    rollback_plan: str = Field(default="", max_length=300)
    manual_change: str | None = Field(default=None, max_length=600)


DEFAULT_ROLLBACK = {
    "patch_container_resources": "Patch the limit back to its previous value, or roll back.",
    "rollback_deployment": "Roll forward to the revision that was current before the rollback.",
    "scale_deployment": "Scale back to the previous replica count.",
    "set_container_image": "Set the image back to the one it replaced, or roll back.",
    "restart_deployment": "A restart needs no rollback; investigate if pods fail again.",
}


def _images(history_output: str) -> list[str]:
    """Images from get_rollout_history output, oldest revision first."""
    try:
        revisions = json.loads(history_output)["revisions"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []
    return [img for r in revisions for img in r.get("images", [])]


def _recent_rollout(events_output: str) -> bool:
    """True if the deployment rolled out within the event window.

    ReplicaSet creation times are not usable: returning to an earlier pod template reuses
    the old ReplicaSet and only bumps its revision. Deployment ScalingReplicaSet events
    are emitted by every rollout, and their ages are right both live and in replay.
    """
    try:
        events = json.loads(events_output)["events"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return False
    window = RECENT_WINDOW.total_seconds()
    return any(
        e.get("reason") == "ScalingReplicaSet" and e.get("age_s", window + 1) <= window
        for e in events
    )


def _current_memory_mi(state: IncidentState, component: str) -> int | None:
    for item in reversed(state.evidence):
        if component in json.dumps(item.args) or component in item.summary:
            match = _LIMIT.search(item.summary + " " + item.excerpt)
            if match:
                return int(match.group(1))
    return None


# Env values are hidden from the agent, so the memory a service needs is usually unknown.
# One generous step (bounded by the actions server) beats a second incident cycle.
MEMORY_FACTOR = 4
MAX_MEMORY_MI = 512


def default_action(
    action_type: ActionName,
    namespace: str,
    deployment: str,
    state: IncidentState,
    images: list[str],
) -> dict[str, Any]:
    """Conservative parameters for an allowed action when the model's choice is unusable."""
    action: dict[str, Any] = {"type": action_type, "namespace": namespace, "deployment": deployment}
    if action_type == "patch_container_resources":
        current = _current_memory_mi(state, deployment)
        action |= {
            "container": "app",
            "memory_limit": f"{min(max((current or 128) * MEMORY_FACTOR, 256), MAX_MEMORY_MI)}Mi",
        }
    elif action_type == "scale_deployment":
        action["replicas"] = 1
    elif action_type == "set_container_image":
        known_good = [img for img in images[:-1] if img != (images[-1] if images else None)]
        action |= {
            "container": "app",
            "image": known_good[-1] if known_good else (images[0] if images else ""),
        }
    return action


def _defaults_text(action: dict[str, Any]) -> str:
    """Parameters of a default action, for the prompt."""
    skip = {"type", "namespace"}
    return ", ".join(f"{k}={v}" for k, v in action.items() if k not in skip)


def _choice_to_action(choice: RemediationChoice, namespace: str) -> dict[str, Any]:
    action: dict[str, Any] = {
        "type": choice.action_type,
        "namespace": namespace,
        "deployment": choice.deployment,
    }
    if choice.action_type == "patch_container_resources":
        action |= {
            "container": choice.container,
            "memory_limit": choice.memory_limit,
            "cpu_limit": choice.cpu_limit,
        }
    elif choice.action_type == "scale_deployment":
        action["replicas"] = choice.replicas if choice.replicas is not None else 1
    elif choice.action_type == "set_container_image":
        action |= {"container": choice.container, "image": choice.image}
    elif choice.action_type == "rollback_deployment" and choice.to_revision:
        action["to_revision"] = choice.to_revision
    return {k: v for k, v in action.items() if v is not None}


_VERBS: dict[str, tuple[str, ...]] = {
    "scale_deployment": ("scale", "replicas"),
    "rollback_deployment": ("rollback", "rollout undo", "roll back"),
    "restart_deployment": ("restart", "delete pod"),
    "patch_container_resources": ("limit", "resources", "patch"),
    "set_container_image": ("image", "set image"),
}


def injection_conflict(action: dict[str, Any], flags: list[SecurityFlag]) -> SecurityFlag | None:
    """A flag whose text asks for this same kind of change to this same deployment."""
    verbs = _VERBS.get(action["type"], ())
    for flag in flags:
        text = flag.excerpt.lower()
        if action["deployment"].lower() in text and any(v in text for v in verbs):
            return flag
    return None


def validate_action(
    action: dict[str, Any], category: RootCauseCategory, recent: bool, allowed_ns: list[str]
) -> str:
    """Return the action hash, or raise if any guard rejects the action."""
    check_action_allowed(category, action["type"], recent)
    parsed = parse_action(action)
    check_bounds(parsed, allowed_ns)
    return action_hash(parsed)


async def propose_remediation(state: IncidentState, deps: AgentDeps) -> dict[str, Any]:
    assert state.diagnosis is not None
    assert state.triage is not None
    diagnosis = state.diagnosis
    namespace = state.triage.namespace
    history = await deps.toolbox.call(
        "get_rollout_history", {"namespace": namespace, "name": diagnosis.component}
    )
    events = await deps.toolbox.call(
        "get_events",
        {"namespace": namespace, "involved_object_name": diagnosis.component, "since_minutes": 120},
    )
    recent, images = _recent_rollout(events), _images(history)
    allowed = allowed_actions(diagnosis.root_cause_category, recent)
    metrics = state.metrics.model_copy()
    errors: list[str] = []

    template = prompt("propose")
    deps.events.emit("prompt", node="propose", version=template.version, sha256=template.sha256)
    defaults = {
        a: default_action(a, namespace, diagnosis.component, state, images) for a in allowed
    }
    shown = "\n".join(f"- {a} (defaults: {_defaults_text(d)})" for a, d in defaults.items())
    shown = shown or "(none: use manual_change)"
    text = template.render(
        diagnosis=compact_json(diagnosis),
        evidence_items=evidence_block(state.evidence),
        allowed_actions=shown,
    )
    choice: RemediationChoice | None = None
    try:
        choice = await astructured(
            deps.llm("propose"),
            RemediationChoice,
            [HumanMessage(text)],
            metrics,
            max_repairs=deps.budgets.max_repairs,
            timeout_s=deps.timeout(state.started_at),
        )
    except (StructuredOutputError, TimeoutError) as exc:
        errors.append(f"propose: model failed ({type(exc).__name__}); using defaults")

    if not allowed:
        manual = (choice.manual_change if choice and choice.manual_change else None) or (
            f"No automated action is allowed for {diagnosis.root_cause_category.value}. "
            f"Apply the fix from the cited runbooks to deployment {diagnosis.component} "
            "through the normal change process."
        )
        proposal = RemediationProposal(
            kind="manual_change",
            rationale=(choice.rationale if choice else diagnosis.summary),
            manual_change=manual,
        )
        return {"proposal": proposal, "metrics": metrics, "errors": errors}

    action: dict[str, Any] | None = None
    if choice and choice.action_type != "manual_change":
        candidate = _choice_to_action(choice, namespace)
        try:
            validate_action(
                candidate, diagnosis.root_cause_category, recent, deps.settings.allowed_namespaces
            )
            action = candidate
        except (GuardViolationError, ToolError, ValidationError) as exc:
            errors.append(f"propose: model action rejected by guards ({exc}); using a default")
    if action is None:
        action = default_action(allowed[0], namespace, diagnosis.component, state, images)
    try:
        digest = validate_action(
            action, diagnosis.root_cause_category, recent, deps.settings.allowed_namespaces
        )
    except (GuardViolationError, ToolError, ValidationError) as exc:
        return {
            "proposal": RemediationProposal(
                kind="none", rationale=f"No safe action could be built: {exc}"
            ),
            "metrics": metrics,
            "errors": [*errors, f"propose: default action invalid: {exc}"],
        }

    conflict = injection_conflict(action, state.security_flags)
    if conflict:
        return {
            "proposal": RemediationProposal(
                kind="none",
                action=action,
                rationale=f"The candidate action matches instructions found in untrusted data "
                f"({conflict.source}: {conflict.pattern}); it needs manual review.",
            ),
            "metrics": metrics,
            "errors": errors,
        }

    plan: dict[str, Any] = {"dry_run": "skipped in replay mode (no cluster)"}
    if state.mode == "live" and deps.actions is not None:
        plan = json.loads(await deps.actions.call("plan_action", {"action": action}))
        if "error" in plan:
            return {
                "proposal": RemediationProposal(
                    kind="none",
                    action=action,
                    rationale=f"Dry run rejected the action: {plan['error']}",
                ),
                "metrics": metrics,
                "errors": [*errors, f"plan_action: {plan['error']}"],
            }
        digest = plan.get("action_hash", digest)
    proposal = RemediationProposal(
        kind="action",
        action=action,
        action_hash=digest,
        rationale=(choice.rationale if choice and choice.rationale else diagnosis.summary),
        plan=plan,
        rollback_plan=(
            choice.rollback_plan
            if choice and choice.rollback_plan
            else DEFAULT_ROLLBACK[action["type"]]
        ),
    )
    return {"proposal": proposal, "metrics": metrics, "errors": errors}


# ---- human_approval -----------------------------------------------------------------------------


def approval_request(state: IncidentState) -> dict[str, Any]:
    assert state.proposal is not None
    return {
        "incident_id": state.incident_id,
        "mode": state.mode,
        "diagnosis": state.diagnosis.model_dump(mode="json") if state.diagnosis else None,
        "proposal": state.proposal.model_dump(mode="json"),
        "security_flags": [f.model_dump() for f in state.security_flags],
    }


async def human_approval(state: IncidentState, deps: AgentDeps) -> dict[str, Any]:
    """Pause the graph until a human answers. Nothing before interrupt() has side effects."""
    answer = interrupt(approval_request(state))
    try:
        decision = ApprovalDecision.model_validate(answer)
    except ValidationError as exc:
        reject = ApprovalDecision(
            decision="reject", approver="system", reason=f"invalid decision: {exc}"
        )
        return {"approval": reject, "errors": ["human_approval: invalid decision payload"]}
    deps.events.emit("approval", decision=decision.decision, approver=decision.approver)
    if decision.decision != "edit":
        return {"approval": decision}
    assert state.proposal is not None
    assert state.diagnosis is not None
    original = state.proposal.action or {}
    edited = {**original, **(decision.edited_action or {})}
    try:
        if edited.get("type") != original.get("type"):
            raise GuardViolationError("an edit may change parameters, not the action type")
        # The original action already passed the allowlist; bounds are checked again here.
        digest = validate_action(
            edited,
            state.diagnosis.root_cause_category,
            recent=True,
            allowed_ns=deps.settings.allowed_namespaces,
        )
    except (GuardViolationError, ToolError, ValidationError) as exc:
        reject = decision.model_copy(
            update={"decision": "reject", "reason": f"edited action rejected: {exc}"}
        )
        return {"approval": reject, "errors": [f"human_approval: edited action rejected: {exc}"]}
    proposal = state.proposal.model_copy(update={"action": edited, "action_hash": digest})
    return {"approval": decision, "proposal": proposal}


# ---- execute -------------------------------------------------------------------------------


async def execute(state: IncidentState, deps: AgentDeps) -> dict[str, Any]:
    """Run the approved action with a freshly minted token. Never without a human decision."""
    approval, proposal = state.approval, state.proposal
    if approval is None or approval.decision not in ("approve", "edit") or proposal is None:
        return {"action_result": ActionResult(executed=False, error="not approved")}
    if proposal.kind != "action" or proposal.action is None or proposal.action_hash is None:
        return {"action_result": ActionResult(executed=False, error="no executable action")}
    if state.mode == "replay" or deps.actions is None:
        return {
            "action_result": ActionResult(
                executed=False,
                simulated=True,
                action=proposal.action,
                output={"note": "replay mode: the approved action was not executed"},
            )
        }
    token = mint_approval_token(proposal.action_hash, approval.approver, settings=deps.settings)
    raw = await deps.actions.call(
        "execute_action", {"action": proposal.action, "approval_token": token}
    )
    output = json.loads(raw)
    deps.events.emit("execute", ok="error" not in output, action=proposal.action)
    if "error" in output:
        return {
            "action_result": ActionResult(
                executed=False, action=proposal.action, error=json.dumps(output["error"])
            ),
            "errors": [f"execute_action: {output['error']}"],
        }
    return {"action_result": ActionResult(executed=True, action=proposal.action, output=output)}


# ---- verify --------------------------------------------------------------------------------


def _healthy(deployment_output: str, pods_output: str) -> tuple[bool, str]:
    try:
        d = json.loads(deployment_output)
        pods = json.loads(pods_output).get("pods", [])
        r = d["replicas"]
    except (json.JSONDecodeError, KeyError, AttributeError):
        return False, "could not read deployment status"
    failing = [p["name"] for p in pods if p.get("reason") and p.get("reason") != "Terminating"]
    ready = r["desired"] > 0 and r["ready"] >= r["desired"] and r["updated"] >= r["desired"]
    return ready and not failing, f"{d['name']}: {r['ready']}/{r['desired']} ready" + (
        f", failing pods {failing}" if failing else ""
    )


async def verify(state: IncidentState, deps: AgentDeps) -> dict[str, Any]:
    """Poll read tools until the affected deployments are healthy (live runs only)."""
    result = state.action_result
    if state.mode == "replay" or (result and result.simulated):
        return {
            "verification": Verification(
                status="not_applicable", details="replay mode: nothing was changed"
            )
        }
    if result is None or not result.executed:
        return {"verification": Verification(status="skipped", details="no action was executed")}
    assert state.triage is not None
    assert state.diagnosis is not None
    namespace = state.triage.namespace
    targets = list(dict.fromkeys([state.diagnosis.component, state.triage.service]))
    sleep = deps.sleep or asyncio.sleep
    start = deps.clock()
    details = ""
    while True:
        statuses = []
        for name in targets:
            dep = await deps.toolbox.call("get_deployment", {"namespace": namespace, "name": name})
            pods = await deps.toolbox.call(
                "list_pods",
                {"namespace": namespace, "label_selector": f"app.kubernetes.io/name={name}"},
            )
            statuses.append(_healthy(dep, pods))
        details = "; ".join(text for _, text in statuses)
        elapsed = deps.clock() - start
        if all(ok for ok, _ in statuses):
            return {
                "verification": Verification(
                    status="resolved", details=details, elapsed_s=round(elapsed, 1)
                )
            }
        if elapsed >= deps.budgets.verify_s:
            return {
                "verification": Verification(
                    status="not_resolved", details=details, elapsed_s=round(elapsed, 1)
                )
            }
        await sleep(deps.budgets.verify_interval_s)


# ---- report ---------------------------------------------------------------------------------


class FollowUps(BaseModel):
    follow_ups: list[str] = Field(min_length=1, max_length=4)


def _outcome(state: IncidentState) -> str:
    if state.escalation_reason:
        return f"Escalated to a human: {state.escalation_reason}. No action taken."
    parts = []
    if state.proposal:
        parts.append(f"Proposed {state.proposal.kind}.")
    if state.approval:
        parts.append(f"Human decision: {state.approval.decision}.")
    if state.action_result:
        parts.append("Executed." if state.action_result.executed else "Not executed.")
    if state.verification:
        parts.append(f"Verification: {state.verification.status}.")
    return " ".join(parts)


def render_report(state: IncidentState, follow_ups: list[str], generated_at: str) -> str:
    alert, diagnosis = state.alert, state.diagnosis
    title = alert.name if alert else "incident"
    lines = [f"# Incident {state.incident_id}: {title}", ""]
    if state.escalation_reason:
        lines += [f"> **Needs human:** {state.escalation_reason}. OpsPilot took no action.", ""]
    lines += [
        f"- Mode: {state.mode}{'' if state.use_rag else ' (retrieval disabled)'}",
        f"- Alert: {alert.summary if alert else '-'}",
        f"- Report generated: {generated_at}",
        "",
        "## Timeline",
        "1. Alert received and triaged"
        + (f": {state.triage.service} in {state.triage.namespace}." if state.triage else "."),
        f"2. Retrieved {len(state.retrieved)} runbook chunks.",
        f"3. Collected {len(state.evidence)} evidence items with "
        f"{state.metrics.tool_calls} tool calls.",
        "4. Diagnosis: "
        + (
            f"{diagnosis.root_cause_category.value} ({diagnosis.confidence:.2f})."
            if diagnosis
            else "none."
        ),
    ]
    step = 5
    if state.proposal:
        lines.append(f"{step}. Proposal: {state.proposal.kind}.")
        step += 1
    if state.approval:
        lines.append(f"{step}. {state.approval.decision} by {state.approval.approver}.")
        step += 1
    if state.action_result:
        lines.append(
            f"{step}. Action {'executed' if state.action_result.executed else 'not executed'}."
        )
        step += 1
    if state.verification:
        lines.append(f"{step}. Verification: {state.verification.status}.")
    lines.append("")
    if diagnosis:
        lines += [
            f"## Root cause: {diagnosis.root_cause_category.value} "
            f"(confidence {diagnosis.confidence:.2f})",
            f"Component: `{diagnosis.component}`",
            "",
            diagnosis.summary,
            "",
        ]
        cited = [c for c in state.retrieved if c.citation_id in diagnosis.runbook_refs]
        for c in cited:
            lines.append(f"- [{c.citation_id}] {c.title} (`{c.doc_id}`, {c.section or 'overview'})")
        if diagnosis.alternatives:
            lines += ["", "Alternatives considered:"]
            lines += [f"- {a.category.value}: {a.why_less_likely}" for a in diagnosis.alternatives]
        lines.append("")
    lines += ["## Evidence", "", "| id | tool | summary |", "|---|---|---|"]
    lines += [f"| {e.id} | `{e.tool}` | {e.summary.replace('|', '/')} |" for e in state.evidence]
    lines.append("")
    if state.proposal:
        p = state.proposal
        lines += [f"## Remediation: {p.kind}", "", p.rationale, ""]
        if p.action:
            lines += ["```json", json.dumps(p.action, indent=2), "```", ""]
            if p.plan.get("diff"):
                lines.append("Dry-run diff:")
                lines += [f"- `{d['path']}`: {d['before']} -> {d['after']}" for d in p.plan["diff"]]
            else:
                lines.append(f"Dry run: {p.plan.get('dry_run', '-')}")
            lines += [f"- Risk: {n}" for n in p.plan.get("risk_notes", [])]
            lines += ["", f"Rollback plan: {p.rollback_plan}", ""]
        if p.manual_change:
            lines += ["Suggested manual change:", "", p.manual_change, ""]
    if state.approval:
        a = state.approval
        lines += [f"## Approval: {a.decision} by {a.approver}", "", a.reason or "-", ""]
    if state.action_result:
        r = state.action_result
        status = "executed" if r.executed else ("simulated" if r.simulated else "not executed")
        lines += [f"## Action result: {status}", "", r.error or json.dumps(r.output)[:600], ""]
    if state.verification:
        v = state.verification
        lines += [f"## Verification: {v.status}", "", f"{v.details} ({v.elapsed_s:.0f} s)", ""]
    lines += ["## Follow-ups", ""] + [f"- {f}" for f in follow_ups] + [""]
    lines += ["## Security flags", ""]
    lines += [
        f"- `{f.pattern}` in {f.source}: {f.excerpt!r} (flagged, not acted on)"
        for f in state.security_flags
    ] or ["None."]
    m = state.metrics
    lines += [
        "",
        "## Run metrics",
        "",
        f"{m.llm_calls} LLM calls, {m.tokens_in} input / {m.tokens_out} output tokens, "
        f"{m.tool_calls} tool calls.",
    ]
    if state.errors:
        lines += ["", "## Errors", ""] + [f"- {e}" for e in state.errors]
    return "\n".join(lines) + "\n"


async def report(state: IncidentState, deps: AgentDeps) -> dict[str, Any]:
    metrics = state.metrics.model_copy()
    follow_ups = ["Review this incident and add the missing check or alert to the runbook."]
    if state.diagnosis:
        template = prompt("report")
        deps.events.emit("prompt", node="report", version=template.version, sha256=template.sha256)
        text = template.render(diagnosis=compact_json(state.diagnosis), outcome=_outcome(state))
        try:
            result = await astructured(
                deps.llm("report"),
                FollowUps,
                [HumanMessage(text)],
                metrics,
                max_repairs=1,
                timeout_s=deps.timeout(state.started_at),
            )
            follow_ups = result.follow_ups
        except (StructuredOutputError, TimeoutError):
            pass
    final = state.model_copy(update={"metrics": metrics})
    generated = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    markdown = render_report(final, follow_ups, generated)
    path = deps.report_dir / state.incident_id / "report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown)
    return {
        "report": IncidentReport(path=str(path), markdown=markdown, needs_human=state.needs_human),
        "metrics": metrics,
    }
