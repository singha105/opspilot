"""Investigation stage: ingest the alert, triage, retrieve runbooks, gather evidence."""

import asyncio
import json
import re
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field, create_model

from opspilot.agent.deps import AgentDeps
from opspilot.agent.evidence import (
    SUMMARY_MAX,
    failure_signals,
    notable_log_lines,
    pods_from_evidence,
    summarize,
    top_error_line,
)
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
from opspilot.agent.toolbox import INVESTIGATION_TOOLS, READ_TOOLS
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
            result = await asyncio.to_thread(
                retriever.retrieve,
                queries,
                deps.settings.retrieval_k,
                mode=deps.settings.retrieval_mode,
                rerank=deps.settings.agent_rerank,
            )
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


REPEATS_BEFORE_FALLBACK = 2
MAX_SUGGESTIONS = 4


def format_call(name: str, args: dict[str, Any]) -> str:
    shown = ", ".join(f"{k}={v}" for k, v in args.items() if v not in (None, ""))
    return f"{name}({shown})"


_HOST_PORT = re.compile(r"\b([a-z][a-z0-9-]{0,62}):(\d{2,5})\b")
MAX_DEPENDENCIES = 2


def dependencies_in_logs(evidence: list[Evidence], service: str) -> list[str]:
    """Service names that appear as host:port in log evidence (not the service itself)."""
    hosts: list[str] = []
    for item in evidence:
        if item.tool != "get_pod_logs" or item.error:
            continue
        for match in _HOST_PORT.finditer(f"{item.summary}\n{item.excerpt}"):
            host = match.group(1)
            if host not in hosts and host not in (service, "localhost"):
                hosts.append(host)
    return hosts[:MAX_DEPENDENCIES]


def suggest_next_calls(
    evidence: list[Evidence], namespace: str, service: str, seen: set[str]
) -> list[tuple[str, dict[str, Any]]]:
    """Generic next steps an SRE would take, given what is known so far (never repeats)."""
    candidates: list[tuple[str, dict[str, Any]]] = []
    if not any(e.tool == "list_pods" and not e.error for e in evidence):
        candidates.append(("list_pods", {"namespace": namespace}))
    # Services named in error logs (host:port): a caller fails when its dependency is down.
    for host in dependencies_in_logs(evidence, service):
        candidates.append(("get_service_endpoints", {"namespace": namespace, "name": host}))
    # The affected service first, then pods that crashed: callers of a broken service are
    # often unready too, but they are a symptom.
    pods = sorted(
        pods_from_evidence(evidence),
        key=lambda p: (not p.name.startswith(f"{service}-"), not p.restarted),
    )
    for pod in pods:
        if pod.unhealthy:
            candidates.append(("describe_pod", {"namespace": namespace, "name": pod.name}))
            log_args: dict[str, Any] = {"namespace": namespace, "name": pod.name}
            if pod.restarted:
                log_args["previous"] = True
            candidates.append(("get_pod_logs", log_args))
    candidates += [
        ("get_deployment", {"namespace": namespace, "name": service}),
        ("get_rollout_history", {"namespace": namespace, "name": service}),
        ("get_events", {"namespace": namespace}),
    ]
    # A call that already succeeded on the same object with other options counts as done.
    done = {(e.tool, _target(e.args)) for e in evidence if not e.error}
    out = [
        (n, a) for n, a in candidates if call_key(n, a) not in seen and (n, _target(a)) not in done
    ]
    return out[:MAX_SUGGESTIONS]


def _target(args: dict[str, Any]) -> str | None:
    """The object a read call is about (None for namespace-wide calls)."""
    return args.get("name") or args.get("involved_object_name")


def tool_catalog(specs: list[dict[str, Any]]) -> str:
    """Compact text list of the tools and their parameters for the prompt."""
    lines = []
    for spec in specs:
        fn = spec["function"]
        props = fn.get("parameters", {}).get("properties", {})
        required = set(fn.get("parameters", {}).get("required", []))
        params = ", ".join(f"{name}{'' if name in required else '?'}" for name in props)
        lines.append(f"- {fn['name']}({params}): {fn.get('description', '')}")
    return "\n".join(lines)


def decision_model(names: list[str]) -> type[BaseModel]:
    """Schema for one investigation step; the tool name is limited to the real tools."""
    return create_model(
        "NextStep",
        tool=(Literal[tuple(names)], Field(description="The tool to call next.")),
        args=(dict[str, Any], Field(default_factory=dict, description="Arguments for the tool.")),
        reason=(str, Field(default="", max_length=300)),
    )


async def _next_call_json(
    deps: AgentDeps, decision: type[BaseModel], text: str, metrics: Any, timeout: float
) -> tuple[str, dict[str, Any]] | None:
    """Constrained-JSON step: the grammar forces an answer straight away (no long thinking)."""
    try:
        step = await astructured(
            deps.llm("select_tool"),
            decision,
            [HumanMessage(text)],
            metrics,
            max_repairs=1,
            timeout_s=timeout,
        )
    except StructuredOutputError:
        return None
    return str(step.tool), dict(step.args)  # type: ignore[attr-defined]


async def _next_call_native(
    model: Any, text: str, metrics: Any, timeout: float
) -> tuple[str, dict[str, Any]] | None:
    """Native tool calling (for models that do not reason at length before each call)."""
    reply = await asyncio.wait_for(model.ainvoke([HumanMessage(text)]), timeout)
    metrics.add_usage(getattr(reply, "usage_metadata", None))
    tool_calls = reply.tool_calls if isinstance(reply, AIMessage) else []
    if not tool_calls:
        return None
    return tool_calls[0]["name"], dict(tool_calls[0].get("args") or {})


async def investigate(state: IncidentState, deps: AgentDeps) -> dict[str, Any]:
    """Tool loop: one tool per turn, evidence summaries in the prompt, hard budgets."""
    assert state.alert is not None
    assert state.triage is not None
    template = prompt("investigate")
    deps.events.emit("prompt", node="investigate", version=template.version, sha256=template.sha256)
    # Without RAG the knowledge-base tool goes too, so the ablation is tools-only.
    names_wanted = INVESTIGATION_TOOLS if state.use_rag else READ_TOOLS
    specs = [*deps.toolbox.specs(names_wanted), FINISH_SPEC]
    names = [s["function"]["name"] for s in specs]
    allowed = set(names)
    native = deps.settings.agent_tool_strategy == "native"
    model = deps.llm("investigate").bind_tools(specs) if native else None
    decision = decision_model(names)
    catalog = tool_catalog(specs)
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

    made: list[str] = []
    repeats = 0
    namespace, service = state.triage.namespace, state.triage.service

    for _turn in range(max_turns):
        if calls >= deps.budgets.tool_calls:
            notes.append("tool budget reached")
            break
        if deps.remaining(state.started_at) < deps.budgets.llm_timeout_s / 3:
            errors.append("investigation stopped early: run time budget nearly used")
            break
        suggestions = suggest_next_calls(evidence, namespace, service, seen)
        evidence_text = evidence_block(evidence)
        if notes:
            evidence_text += "\nNotes: " + " ".join(notes[-2:])
        text = template.render(
            alert=alert_block(state.alert),
            triage=compact_json(state.triage),
            hints=hints,
            evidence=evidence_text,
            calls_made=", ".join(made) or "(none yet)",
            suggestions="\n".join(f"- {format_call(n, a)}" for n, a in suggestions) or "(none)",
            budget_left=str(deps.budgets.tool_calls - calls),
            namespace=namespace,
            tools=catalog,
        )
        timeout = deps.timeout(state.started_at)
        try:
            chosen = (
                await _next_call_native(model, text, metrics, timeout)
                if native
                else await _next_call_json(deps, decision, text, metrics, timeout)
            )
        except TimeoutError:
            errors.append("investigate: model call timed out")
            break
        if chosen is None:
            notes.append("Your last reply had no tool call; call a tool or finish_investigation.")
            continue
        name, args = chosen
        if name == FINISH:
            deps.events.emit("investigation_finished", reason=str(args.get("reason", ""))[:200])
            break
        if name not in allowed:
            notes.append(f"'{name}' is not an available tool.")
            continue
        key = call_key(name, args)
        if key in seen:
            repeats += 1
            notes.append(f"Blocked: {format_call(name, args)} was already called.")
            deps.events.emit("tool_blocked", tool=name, args=args)
            if repeats < REPEATS_BEFORE_FALLBACK or not suggestions:
                continue
            # A small model at temperature 0 can get stuck on one call: take the top
            # suggestion instead (always a read-only call within the same budget).
            name, args = suggestions[0]
            key = call_key(name, args)
            deps.events.emit("tool_auto", tool=name, args=args, reason="repeated blocked call")
        repeats = 0
        seen.add(key)
        made.append(format_call(name, args))
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


# ---- refine_retrieval ------------------------------------------------------------------------

MAX_NEW_CHUNKS = 3


def evidence_queries(state: IncidentState) -> list[str]:
    """Knowledge-base queries built from what the investigation found (empty if nothing)."""
    service = state.triage.service if state.triage else ""
    signals = failure_signals(state.evidence, focus=service or None)
    error = top_error_line(state.evidence)
    queries = [f"{service} {' '.join(signals)}".strip()] if signals else []
    if error:
        queries.append(error)
    return queries


async def refine_retrieval(state: IncidentState, deps: AgentDeps) -> dict[str, Any]:
    """Search the knowledge base again with the investigation's failure signals.

    Retrieval before the investigation only has the alert to go on; the evidence names the
    failure (an OOM kill, a scheduling reason, a missing ConfigMap), so a second search adds
    up to three new chunks, numbered after the existing ones.
    """
    if not state.use_rag or deps.retriever is None:
        return {}
    queries = evidence_queries(state)
    if not queries:
        return {}
    try:
        retriever = deps.retriever()
        try:
            result = await asyncio.to_thread(
                retriever.retrieve,
                queries,
                deps.settings.retrieval_k,
                mode=deps.settings.retrieval_mode,
                rerank=deps.settings.agent_rerank,
            )
        finally:
            close = getattr(getattr(retriever, "store", None), "close", None)
            if callable(close):
                close()
    except Exception as exc:  # optional, like the first retrieval
        deps.events.emit("retrieval_failed", error=type(exc).__name__)
        return {"errors": [f"refine_retrieval: {type(exc).__name__}"]}
    known = {c.chunk_id for c in state.retrieved}
    fresh = [c for c in result.chunks if c.chunk_id not in known][:MAX_NEW_CHUNKS]
    start = len(state.retrieved) + 1
    added = [c.model_copy(update={"citation_id": f"R{start + i}"}) for i, c in enumerate(fresh)]
    flags = [
        flag
        for chunk in added
        for flag in detect_injection(chunk.text, f"kb:{chunk.doc_id}", document=True)
    ]
    deps.events.emit(
        "retrieval", stage="refine", queries=queries, doc_ids=[c.doc_id for c in added]
    )
    return {"retrieved": [*state.retrieved, *added], "security_flags": flags}
