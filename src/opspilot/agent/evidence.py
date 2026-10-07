"""Turn raw tool output into short Evidence items.

Structured tools are summarized deterministically (no LLM, no tokens). Logs get a
deterministic excerpt of the notable lines; the investigate node may add an LLM summary.
"""

import json
import re
from collections import Counter
from collections.abc import Callable
from typing import Any, NamedTuple

from opspilot.models.incident import Evidence

SUMMARY_MAX = 300
EXCERPT_MAX = 800
_NOTABLE = re.compile(
    r"(?i)(error|fatal|exception|traceback|refused|timed? ?out|failed|denied|oom|panic"
    r"|unavailable|warn)"
)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in args.items() if v not in (None, False, ""))


def _pods(data: dict[str, Any]) -> tuple[str, str]:
    pods = data.get("pods", [])
    bad = [
        p for p in pods if not p["ready"].startswith(p["ready"].split("/")[1]) or p.get("reason")
    ]
    lines = [
        f"{p['name']} {p['ready']} {p['phase']} restarts={p['restarts']}"
        + (f" reason={p['reason']}" if p.get("reason") else "")
        + (f" last={p['last_reason']}" if p.get("last_reason") else "")
        for p in pods
    ]
    if not pods:
        summary = "No pods match."
    elif not bad:
        summary = f"{len(pods)} pods, all ready, no failure reasons."
    else:
        summary = f"{len(pods)} pods, {len(bad)} unhealthy: " + "; ".join(
            f"{p['name']} {p['ready']} {p.get('reason') or p['phase']}"
            + (f" (last {p['last_reason']})" if p.get("last_reason") else "")
            for p in bad
        )
    return summary, "\n".join(lines)


def _container(c: dict[str, Any]) -> str:
    state: tuple[str, dict[str, Any]] = next(iter(c.get("state", {}).items()), ("?", {}))
    last = c.get("lastState", {}).get("terminated")
    parts = [
        f"{c['name']}: {state[0]}"
        + (f" {state[1].get('reason')}" if state[1].get("reason") else "")
    ]
    if last:
        parts.append(f"last terminated {last.get('reason')} exit {last.get('exitCode')}")
    parts.append(f"restarts={c.get('restartCount', 0)}")
    limits = c.get("resources", {}).get("limits")
    if limits:
        parts.append("limits " + ",".join(f"{k}={v}" for k, v in limits.items()))
    return "; ".join(parts)


def _describe(data: dict[str, Any]) -> tuple[str, str]:
    containers = data.get("initContainers", []) + data.get("containers", [])
    summary = f"{data['pod']} ready={data['ready']}. " + " | ".join(
        _container(c) for c in containers
    )
    lines = []
    for c in containers:
        lines.append(f"{c['name']} image={c['image']} env={','.join(c.get('envNames', []))}")
        for kind, probe in c.get("probes", {}).items():
            lines.append(f"  {kind} probe: {probe}")
    for e in data.get("events", [])[:5]:
        lines.append(f"event {e['type']} {e['reason']} x{e['count']}: {e['message']}")
    return summary, "\n".join(lines)


def notable_log_lines(lines: list[str], limit: int = 12) -> list[str]:
    """Distinct notable lines (errors first), with repeat counts, most recent kept."""
    picked = [line for line in lines if _NOTABLE.search(line)] or lines[-5:]
    counts = Counter(picked)
    ordered = list(dict.fromkeys(reversed(picked)))[:limit]
    return [f"{line} (x{counts[line]})" if counts[line] > 1 else line for line in reversed(ordered)]


def _logs(data: dict[str, Any]) -> tuple[str, str]:
    lines = data.get("lines", [])
    notable = notable_log_lines(lines)
    which = "previous container" if data.get("previous") else "current container"
    errors = sum(1 for line in lines if _NOTABLE.search(line))
    summary = f"{len(lines)} log lines from {which} of {data['pod']}, {errors} look like errors."
    if notable:
        summary += " Last: " + notable[-1]
    return summary, "\n".join(notable)


def _events(data: dict[str, Any]) -> tuple[str, str]:
    events = data.get("events", [])
    warnings = [e for e in events if e["type"] == "Warning"]
    reasons = Counter(f"{e['reason']} on {e['object']}" for e in warnings)
    summary = f"{len(events)} events, {len(warnings)} warnings"
    if reasons:
        summary += ": " + "; ".join(f"{r} x{n}" for r, n in reasons.most_common(4))
    lines = [
        f"{e['age']} {e['type']} {e['reason']} {e['object']}: {e['message']}" for e in warnings[:8]
    ]
    return summary, "\n".join(lines)


def _deployment(data: dict[str, Any]) -> tuple[str, str]:
    r = data["replicas"]
    problems = [c for c in data.get("conditions", []) if c.get("status") != "True"]
    summary = (
        f"{data['name']} revision {data.get('revision')}: {r['ready']}/{r['desired']} ready, "
        f"{r['updated']} updated, {r['unavailable']} unavailable."
    )
    if problems:
        summary += " " + "; ".join(
            f"{c['type']}={c['status']} ({c.get('reason')})" for c in problems
        )
    lines = [f"strategy {data.get('strategy')}"]
    for c in data["template"]["containers"]:
        lines.append(
            f"{c['name']} image={c['image']} env={','.join(c.get('envNames', []))} "
            f"resources={json.dumps(c.get('resources', {}))}"
        )
        for kind, probe in c.get("probes", {}).items():
            lines.append(f"  {kind} probe: {probe}")
    return summary, "\n".join(lines)


def _history(data: dict[str, Any]) -> tuple[str, str]:
    revisions = data.get("revisions", [])
    lines = [
        f"rev {r['revision']} created {r['created']} images={','.join(r['images'])} "
        f"replicas={r['replicas']} ready={r['ready']}"
        for r in revisions
    ]
    if not revisions:
        return "No rollout history.", ""
    latest = revisions[-1]
    summary = (
        f"{data['deployment']}: {len(revisions)} revisions, "
        f"current {data.get('current_revision')}; latest rev {latest['revision']} "
        f"created {latest['created']} with {','.join(latest['images'])}."
    )
    if len(revisions) > 1 and revisions[-2]["images"] != latest["images"]:
        summary += f" Image changed from {','.join(revisions[-2]['images'])}."
    return summary, "\n".join(lines[-6:])


def _endpoints(data: dict[str, Any]) -> tuple[str, str]:
    summary = (
        f"Service {data['service']}: {data['ready']} ready and "
        f"{data['not_ready']} not-ready endpoints."
    )
    lines = [f"{a['ip']} ready={a['ready']} pod={a.get('pod')}" for a in data.get("addresses", [])]
    return summary, "\n".join(lines)


def _services(data: dict[str, Any]) -> tuple[str, str]:
    services = data.get("services", [])
    lines = [f"{s['name']} selector={s.get('selector')} ports={s.get('ports')}" for s in services]
    return f"{len(services)} services: {', '.join(s['name'] for s in services)}.", "\n".join(lines)


def _configmap(data: dict[str, Any]) -> tuple[str, str]:
    keys = sorted(data.get("data", {}))
    lines = [f"{k}={v}" for k, v in sorted(data.get("data", {}).items())]
    return f"ConfigMap {data['name']} keys: {', '.join(keys)}.", "\n".join(lines)


def _nodes(data: dict[str, Any]) -> tuple[str, str]:
    nodes = data.get("nodes", [])
    lines = []
    for n in nodes:
        bad = [
            c["type"]
            for c in n.get("conditions", [])
            if (c["type"] == "Ready") != (c["status"] == "True")
        ]
        lines.append(
            f"{n['name']} allocatable={n.get('allocatable')} "
            f"taints={n.get('taints')} problems={bad}"
        )
    return f"{len(nodes)} nodes.", "\n".join(lines)


def _pvcs(data: dict[str, Any]) -> tuple[str, str]:
    pvcs = data.get("pvcs", [])
    lines = [f"{p['name']} {p['status']} class={p.get('storage_class')}" for p in pvcs]
    pending = [p["name"] for p in pvcs if p["status"] != "Bound"]
    summary = f"{len(pvcs)} PVCs" + (f", not bound: {', '.join(pending)}." if pending else ".")
    return summary, "\n".join(lines)


def _kb(data: dict[str, Any]) -> tuple[str, str]:
    chunks = data.get("chunks", [])
    summary = "Knowledge base: " + "; ".join(f"{c['doc_id']} ({c['section']})" for c in chunks[:4])
    lines = [f"{c['doc_id']} > {c['section']}: {c['text'][:150]}" for c in chunks[:4]]
    return summary, "\n".join(lines)


SUMMARIZERS: dict[str, Callable[[dict[str, Any]], tuple[str, str]]] = {
    "list_pods": _pods,
    "describe_pod": _describe,
    "get_pod_logs": _logs,
    "get_events": _events,
    "get_deployment": _deployment,
    "get_rollout_history": _history,
    "list_services": _services,
    "get_service_endpoints": _endpoints,
    "get_configmap": _configmap,
    "list_nodes": _nodes,
    "list_pvcs": _pvcs,
    "search_knowledge": _kb,
}


def summarize(tool: str, args: dict[str, Any], output: str) -> tuple[str, str, bool]:
    """(summary, excerpt, is_error) for one tool result, within the Evidence size limits."""
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return (
            _clip(f"{tool}({_args(args)}) returned non-JSON output.", SUMMARY_MAX),
            _clip(output, EXCERPT_MAX),
            True,
        )
    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        text = f"{tool}({_args(args)}) failed: {err.get('type')}: {err.get('message')}"
        return _clip(text, SUMMARY_MAX), _clip(err.get("hint", ""), EXCERPT_MAX), True
    summarizer = SUMMARIZERS.get(tool)
    if summarizer is None:
        return _clip(f"{tool}({_args(args)}) ok.", SUMMARY_MAX), _clip(output, EXCERPT_MAX), False
    try:
        summary, excerpt = summarizer(data)
    except (KeyError, TypeError, IndexError):
        return (
            _clip(f"{tool}({_args(args)}) returned an unexpected shape.", SUMMARY_MAX),
            _clip(output, EXCERPT_MAX),
            True,
        )
    if data.get("truncated"):
        excerpt += "\n(output was truncated by the server)"
    return _clip(summary, SUMMARY_MAX), _clip(excerpt, EXCERPT_MAX), False


class PodHealth(NamedTuple):
    """One pod as seen in a list_pods excerpt, with the evidence id it came from."""

    name: str
    unhealthy: bool
    restarted: bool
    evidence_id: str


def pods_from_evidence(evidence: list[Evidence]) -> list[PodHealth]:
    """Pods from successful list_pods excerpts ('name ready phase restarts=N ...'); latest wins."""
    pods: dict[str, PodHealth] = {}
    for item in evidence:
        if item.tool != "list_pods" or item.error:
            continue
        for line in item.excerpt.splitlines():
            parts = line.split()
            if len(parts) < 4 or "/" not in parts[1] or not parts[3].startswith("restarts="):
                continue
            ready, total = parts[1].split("/", 1)
            restarts = int(parts[3].removeprefix("restarts=") or 0)
            pods[parts[0]] = PodHealth(
                parts[0],
                unhealthy=ready != total or "reason=" in line,
                restarted=restarts > 0 or "last=" in line,
                evidence_id=item.id,
            )
    return list(pods.values())
