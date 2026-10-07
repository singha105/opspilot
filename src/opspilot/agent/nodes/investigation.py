"""Investigation stage: ingest the alert, triage, retrieve runbooks, gather evidence."""

import asyncio
import json
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from opspilot.agent.deps import AgentDeps
from opspilot.agent.evidence import SUMMARY_MAX, notable_log_lines, summarize
from opspilot.agent.guards import detect_injection
from opspilot.agent.nodes.common import (
    CATEGORIES,
    alert_block,
    compact_json,
    evidence_block,
    prompt,
)
from opspilot.agent.prompts import untrusted
from opspilot.agent.state import IncidentState, SecurityFlag
from opspilot.agent.toolbox import INVESTIGATION_TOOLS
from opspilot.llm import StructuredOutputError, astructured
from opspilot.mcp_servers.k8s_readonly.replay import call_key
from opspilot.models import Alert
from opspilot.models.incident import Evidence, Triage
from opspilot.rag.chunking import split_sections
from opspilot.rag.loader import load_document

HINT_CHARS = 600
MAX_HINTS = 3
FINISH = "finish_investigation"
FINISH_SPEC = {
    "type": "function",
    "function": {
        "name": FINISH,
        "description": "Stop investigating: the evidence explains the symptom, or more calls "
        "would not change the conclusion.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "Why you are done."}},
            "required": ["reason"],
        },
    },
}
_SEVERITY = {"critical": "critical", "page": "critical", "warning": "warning", "info": "info"}


# ---- ingest_alert ---------------------------------------------------------------------------


def normalize_alert(raw: Any) -> Alert:
    """Alertmanager webhook JSON, an Alert-shaped dict, JSON text or plain text -> Alert."""
    if isinstance(raw, Alert):
        return raw
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            text = raw.strip()
            labels = {}
            namespace = re.search(r"\bnamespace[:= ]+([a-z0-9-]+)", text, re.IGNORECASE)
            if namespace:
                labels["namespace"] = namespace.group(1)
            return Alert(
                name="ManualReport",
                severity="warning",
                summary=text[:500] or "(empty)",
                labels=labels,
            )
    if isinstance(raw, dict) and isinstance(raw.get("alerts"), list) and raw["alerts"]:
        first = raw["alerts"][0]
        labels = {str(k): str(v) for k, v in (first.get("labels") or {}).items()}
        annotations = {str(k): str(v) for k, v in (first.get("annotations") or {}).items()}
        return Alert(
            name=labels.pop("alertname", "Alert"),
            severity=_SEVERITY.get(labels.pop("severity", "warning").lower(), "warning"),
            summary=annotations.get("summary") or annotations.get("description") or "(no summary)",
            labels=labels,
            annotations=annotations,
        )
    return Alert.model_validate(raw)


async def ingest_alert(state: IncidentState, deps: AgentDeps) -> dict[str, Any]:
    alert = normalize_alert(state.raw_alert)
    text = " ".join([alert.summary, *alert.annotations.values()])
    return {"alert": alert, "security_flags": detect_injection(text, "alert")}


# ---- triage ----------------------------------------------------------------------------------


def fallback_triage(alert: Alert) -> Triage:
    """Deterministic triage from labels when the model cannot produce one."""
    service = alert.labels.get("deployment") or alert.labels.get("service") or "unknown"
    return Triage(
        service=service,
        namespace=alert.labels.get("namespace", "default"),
        symptom_summary=alert.summary[:300],
        candidate_categories=["UNKNOWN"],
        search_queries=[alert.summary[:120], f"{alert.name} {service}"],
    )


async def triage(state: IncidentState, deps: AgentDeps) -> dict[str, Any]:
    assert state.alert is not None
    template = prompt("triage")
    deps.events.emit("prompt", node="triage", version=template.version, sha256=template.sha256)
    text = template.render(alert=alert_block(state.alert), categories=CATEGORIES)
    metrics = state.metrics.model_copy()
    try:
        result = await astructured(
            deps.llm("triage"),
            Triage,
            [HumanMessage(text)],
            metrics,
            max_repairs=deps.budgets.max_repairs,
            timeout_s=deps.timeout(state.started_at),
        )
        return {"triage": result, "metrics": metrics}
    except (StructuredOutputError, TimeoutError) as exc:
        return {
            "triage": fallback_triage(state.alert),
            "metrics": metrics,
            "errors": [f"triage fell back to labels: {exc}"],
        }


# ---- retrieve --------------------------------------------------------------------------------


def quick_check_hints(state_chunks: list[Any], knowledge_dir: Any) -> list[str]:
    """'Quick checks' sections of the retrieved runbooks, as hints for investigate."""
    hints: list[str] = []
    seen: set[str] = set()
    for chunk in state_chunks:
        if chunk.doc_type != "runbook" or chunk.doc_id in seen or len(hints) >= MAX_HINTS:
            continue
        seen.add(chunk.doc_id)
        path = knowledge_dir / "runbooks" / f"{chunk.doc_id}.md"
        if not path.is_file():
            continue
        doc = load_document(path)
        for section, text in split_sections(doc.body):
            if section and section[-1].lower().startswith("quick checks"):
                hints.append(f"{doc.title} ({chunk.citation_id}):\n{text[:HINT_CHARS]}")
                break
    return hints


async def retrieve(state: IncidentState, deps: AgentDeps) -> dict[str, Any]:
    if not state.use_rag or deps.retriever is None:
        return {"retrieved": [], "hints": []}
    assert state.alert is not None
    assert state.triage is not None
    queries = [*state.triage.search_queries, state.alert.summary]
    try:
        retriever = deps.retriever()
        try:
            result = await asyncio.to_thread(retriever.retrieve, queries, 6)
        finally:
            close = getattr(getattr(retriever, "store", None), "close", None)
            if callable(close):
                close()
    except Exception as exc:  # the knowledge base is optional: investigate without it
        deps.events.emit("retrieval_failed", error=type(exc).__name__)
        return {"retrieved": [], "hints": [], "errors": [f"retrieve: {type(exc).__name__}"]}
    chunks = list(result.chunks)
    flags = [
        flag
        for chunk in chunks
        for flag in detect_injection(chunk.text, f"kb:{chunk.doc_id}", document=True)
    ]
    hints = quick_check_hints(chunks, deps.settings.knowledge_dir)
    deps.events.emit("retrieval", doc_ids=[c.doc_id for c in chunks], timings_ms=result.timings_ms)
    return {"retrieved": chunks, "hints": hints, "security_flags": flags}


# ---- investigate -----------------------------------------------------------------------------


class LogSummary(BaseModel):
    summary: str = Field(max_length=SUMMARY_MAX)


async def _log_summary(
    state: IncidentState, deps: AgentDeps, args: dict[str, Any], output: str, metrics: Any
) -> str | None:
    """Two-sentence LLM summary of the notable log lines (None if it fails)."""
    try:
        lines = json.loads(output).get("lines", [])
    except (json.JSONDecodeError, AttributeError):
        return None
    if not lines:
        return None
    template = prompt("log_summary")
    text = template.render(
        target=f"{args.get('name')} ({'previous' if args.get('previous') else 'current'} "
        "container)",
        log_lines=untrusted("logs", "\n".join(notable_log_lines(lines))),
    )
    try:
        result = await astructured(
            deps.llm("summarize"),
            LogSummary,
            [HumanMessage(text)],
            metrics,
            max_repairs=1,
            timeout_s=deps.timeout(state.started_at),
        )
    except (StructuredOutputError, TimeoutError):
        return None
    return result.summary


async def investigate(state: IncidentState, deps: AgentDeps) -> dict[str, Any]:
    """Tool loop: one tool per turn, evidence summaries in the prompt, hard budgets."""
    assert state.alert is not None
    assert state.triage is not None
    template = prompt("investigate")
    deps.events.emit("prompt", node="investigate", version=template.version, sha256=template.sha256)
    specs = [*deps.toolbox.specs(INVESTIGATION_TOOLS), FINISH_SPEC]
    model = deps.llm("investigate").bind_tools(specs)
    allowed = {s["function"]["name"] for s in specs}
    metrics = state.metrics.model_copy()
    evidence = list(state.evidence)
    new_evidence: list[Evidence] = []
    flags: list[SecurityFlag] = []
    errors: list[str] = []
    notes: list[str] = []
    seen: set[str] = set()
    calls = 0
    max_turns = deps.budgets.tool_calls + 3  # extra turns absorb blocked or malformed calls
    hints = untrusted("runbook_hints", "\n\n".join(state.hints)) if state.hints else "(none)"

    for _turn in range(max_turns):
        if calls >= deps.budgets.tool_calls:
            notes.append("tool budget reached")
            break
        if deps.remaining(state.started_at) < deps.budgets.llm_timeout_s / 3:
            errors.append("investigation stopped early: run time budget nearly used")
            break
        evidence_text = evidence_block(evidence)
        if notes:
            evidence_text += "\nNotes: " + " ".join(notes[-2:])
        text = template.render(
            alert=alert_block(state.alert),
            triage=compact_json(state.triage),
            hints=hints,
            evidence=evidence_text,
            budget_left=str(deps.budgets.tool_calls - calls),
            namespace=state.triage.namespace,
        )
        try:
            reply = await asyncio.wait_for(
                model.ainvoke([HumanMessage(text)]), deps.timeout(state.started_at)
            )
        except TimeoutError:
            errors.append("investigate: model call timed out")
            break
        metrics.add_usage(getattr(reply, "usage_metadata", None))
        tool_calls = reply.tool_calls if isinstance(reply, AIMessage) else []
        if not tool_calls:
            notes.append("Your last reply had no tool call; call a tool or finish_investigation.")
            continue
        call = tool_calls[0]
        name, args = call["name"], dict(call.get("args") or {})
        if name == FINISH:
            deps.events.emit("investigation_finished", reason=str(args.get("reason", ""))[:200])
            break
        if name not in allowed:
            notes.append(f"'{name}' is not an available tool.")
            continue
        key = call_key(name, args)
        if key in seen:
            notes.append(f"Blocked: {name} was already called with these arguments.")
            deps.events.emit("tool_blocked", tool=name, args=args)
            continue
        seen.add(key)
        output = await deps.toolbox.call(name, args)
        calls += 1
        metrics.tool_calls += 1
        deps.events.emit("tool_call", tool=name, args=args, bytes=len(output))
        flags += detect_injection(
            output, f"{name}:{args.get('name') or args.get('namespace') or ''}"
        )
        summary, excerpt, is_error = summarize(name, args, output)
        if name == "get_pod_logs" and not is_error:
            llm_summary = await _log_summary(state, deps, args, output, metrics)
            if llm_summary:
                summary = llm_summary
        item = Evidence(
            id=f"E{len(evidence) + 1}",
            tool=name,
            args=args,
            summary=summary,
            excerpt=excerpt,
            error=is_error,
        )
        evidence.append(item)
        new_evidence.append(item)

    return {"evidence": new_evidence, "security_flags": flags, "errors": errors, "metrics": metrics}
