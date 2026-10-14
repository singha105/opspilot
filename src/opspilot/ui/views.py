"""Rendering helpers for the operator console.

Alert text, tool output, retrieved chunks and model output are untrusted: they are shown
as code or escaped Markdown, never as HTML, so a planted link or script cannot render.
The only links the console adds are its own anchors from ``[E1]``/``[R1]`` references.
"""

import json
import re
from collections.abc import Callable
from typing import Any

import streamlit as st

from opspilot.service import IncidentView

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~])")
_REF = re.compile(r"\\\[([ER]\d+)\\\]")
DECISION_LABELS = {"approve": "Approved", "reject": "Rejected", "edit": "Approved with edits"}
STATUS_LABELS = {
    "running": "Investigating",
    "awaiting_approval": "Waiting for approval",
    "reported": "Reported",
    "escalated": "Escalated to a human",
}


def escape_md(text: str) -> str:
    """Escape Markdown so untrusted text renders literally."""
    return _MD_SPECIAL.sub(r"\\\1", text)


def link_refs(text: str) -> str:
    """Escaped text in which [E1]/[R1] become links to the evidence and chunk anchors."""
    return _REF.sub(lambda m: f"[[{m.group(1)}]](#{m.group(1).lower()})", escape_md(text))


def anchor(ref: str) -> None:
    """An in-page anchor for a reference id (a fixed, safe id)."""
    if re.fullmatch(r"[ER]\d+", ref):
        st.markdown(f'<a id="{ref.lower()}"></a>', unsafe_allow_html=True)


def clean_section(section: str) -> str:
    """A section title without Markdown heading ids such as ``{#container-restarts}``."""
    return re.sub(r"\s*\{#[^}]*\}", "", section).strip()


def call_text(tool: str, args: dict[str, Any]) -> str:
    shown = ", ".join(f"{k}={v}" for k, v in args.items() if v not in (None, False, ""))
    return f"{tool}({shown})"


def diff_rows(plan: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"field": str(c.get("path")), "before": str(c.get("before")), "after": str(c.get("after"))}
        for c in plan.get("diff") or []
    ]


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status.replace("_", " ").capitalize())


class StatusSink:
    """Streams agent progress into a Streamlit status container."""

    def __init__(self, container: Any) -> None:
        self.container = container

    def emit(self, kind: str, **data: Any) -> None:
        if kind == "node_start":
            self.container.update(label=f"Running {data['node']}…")
        elif kind == "node_end":
            extra = f" · {data['tool_calls']} tool calls" if data.get("tool_calls") else ""
            self.container.write(f"✓ `{data['node']}` {data['ms'] / 1000:.1f} s{extra}")
        elif kind == "tool_call":
            self.container.write(f"  · `{call_text(data['tool'], data['args'])}`")
        elif kind == "tool_blocked":
            self.container.write(f"  · blocked repeat of `{data['tool']}`")


# ---- incident panels ------------------------------------------------------------------------


def render_incident(view: IncidentView, decide: Callable[[str, str, str], None] | None) -> None:
    """All panels for one incident; ``decide(decision, approver, reason)`` resumes it."""
    state = view.state or {}
    alert = state.get("alert") or {}
    st.subheader(f"{view.incident_id}", anchor=False)
    st.caption(
        f"{status_label(view.status)} · {view.meta.mode} mode · source: {view.meta.source}"
        + (f" · scenario `{view.meta.scenario}`" if view.meta.scenario else "")
    )
    if alert:
        st.markdown(
            f"**Alert** {escape_md(alert.get('name', ''))}: {escape_md(alert.get('summary', ''))}"
        )
    if not view.state:
        st.info("The agent is still investigating. Refresh in a minute.")
        return
    for flag in state.get("security_flags") or []:
        st.warning(
            f"Possible prompt injection ({flag['pattern']}) in {flag['source']}; flagged and not "
            "acted on.",
            icon="⚠️",
        )
    _triage(state)
    _knowledge(state)
    _evidence(state)
    _diagnosis(state)
    _proposal(view, state, decide)
    _outcome(state)
    _report(view)


def _triage(state: dict[str, Any]) -> None:
    triage = state.get("triage")
    if not triage:
        return
    with st.expander("Triage", expanded=False):
        st.markdown(
            f"**Service** `{triage['service']}` in `{triage['namespace']}`  \n"
            f"**Symptom** {escape_md(triage['symptom_summary'])}  \n"
            f"**Candidate categories** {', '.join(triage['candidate_categories'])}"
        )
        st.caption("Search queries: " + " · ".join(escape_md(q) for q in triage["search_queries"]))


def _knowledge(state: dict[str, Any]) -> None:
    chunks = state.get("retrieved") or []
    st.markdown(f"#### Retrieved knowledge ({len(chunks)})")
    if not chunks:
        st.caption("No retrieval for this run.")
    for chunk in chunks:
        anchor(chunk["citation_id"])
        with st.expander(
            f"{chunk['citation_id']} · {chunk['title']} — {clean_section(chunk['section'])} "
            f"({chunk['doc_type']})"
        ):
            st.caption(f"`{chunk['doc_id']}` · score {chunk['score']:.3f}")
            st.text(chunk["text"])


def _evidence(state: dict[str, Any]) -> None:
    evidence = state.get("evidence") or []
    st.markdown(f"#### Evidence ({len(evidence)} read-only tool calls)")
    for item in evidence:
        anchor(item["id"])
        mark = " (error)" if item.get("error") else ""
        with st.expander(f"{item['id']} · {call_text(item['tool'], item['args'])}{mark}"):
            st.text(item["summary"])
            if item.get("excerpt"):
                st.code(item["excerpt"], language=None)


def _diagnosis(state: dict[str, Any]) -> None:
    st.markdown("#### Diagnosis")
    diagnosis = state.get("diagnosis")
    if state.get("escalation_reason"):
        st.warning(f"Escalated to a human: {state['escalation_reason']}")
    if not diagnosis:
        st.caption("No diagnosis.")
        return
    left, right = st.columns([2, 1])
    left.markdown(f"**{diagnosis['root_cause_category']}** in `{diagnosis['component']}`")
    right.progress(float(diagnosis["confidence"]), text=f"confidence {diagnosis['confidence']:.2f}")
    st.markdown(link_refs(diagnosis["summary"]))
    for alt in diagnosis.get("alternatives") or []:
        st.caption(f"Considered {alt['category']}: {link_refs(alt['why_less_likely'])}")


def _proposal(
    view: IncidentView, state: dict[str, Any], decide: Callable[[str, str, str], None] | None
) -> None:
    proposal = state.get("proposal")
    if not proposal:
        return
    st.markdown("#### Proposed action")
    if proposal["kind"] == "manual_change":
        st.info("No allowlisted action fits; a person should make this change:")
        st.code(proposal.get("manual_change") or "", language=None)
    elif proposal["kind"] == "none":
        st.info("Nothing to execute.")
    else:
        st.code(json.dumps(proposal["action"], indent=1), language="json")
    if proposal.get("rationale"):
        st.markdown(f"**Why** {link_refs(proposal['rationale'])}")
    plan = proposal.get("plan") or {}
    rows = diff_rows(plan)
    if rows:
        st.markdown("**Dry-run diff** (server-side, nothing changed yet)")
        st.table(rows)
    elif plan.get("dry_run"):
        st.caption(f"Dry run: {plan['dry_run']}")
    for note in plan.get("risk_notes") or []:
        st.markdown(f"- **Risk** {escape_md(note)}")
    if proposal.get("rollback_plan"):
        st.markdown(f"**Rollback plan** {escape_md(proposal['rollback_plan'])}")
    if view.pending and decide:
        _approval_form(view.incident_id, decide)


def _approval_form(incident_id: str, decide: Callable[[str, str, str], None]) -> None:
    with st.form(f"approval-{incident_id}", border=True):
        st.markdown(
            "**Your decision.** Approving mints a single-use token for exactly this action."
        )
        approver = st.text_input("Approver", placeholder="your name")
        reason = st.text_input("Reason", placeholder="optional for approve, expected for reject")
        approve_col, reject_col = st.columns(2)
        approve = approve_col.form_submit_button(
            "Approve", type="primary", use_container_width=True
        )
        reject = reject_col.form_submit_button("Reject", use_container_width=True)
    if approve or reject:
        if not approver.strip():
            st.error("Enter the approver's name.")
            return
        decide("approve" if approve else "reject", approver.strip(), reason.strip())


def _outcome(state: dict[str, Any]) -> None:
    approval, result, verification = (
        state.get("approval"),
        state.get("action_result"),
        state.get("verification"),
    )
    if not (approval or result or verification):
        return
    st.markdown("#### Outcome")
    if approval:
        verb = DECISION_LABELS.get(approval["decision"], approval["decision"])
        st.markdown(
            f"**{verb}** by {escape_md(approval['approver'])}"
            + (f": {escape_md(approval['reason'])}" if approval.get("reason") else "")
        )
    if result:
        if result.get("simulated"):
            st.caption("Replay mode: the approved action was simulated, not executed.")
        elif result.get("executed"):
            st.success("Executed through the gated actions server.")
        elif result.get("error"):
            st.error(f"Not executed: {result['error']}")
    if verification:
        text = f"Verification: **{verification['status']}** — {escape_md(verification['details'])}"
        (st.success if verification["status"] == "resolved" else st.info)(text)


def _report(view: IncidentView) -> None:
    if not view.report_markdown:
        return
    with st.expander("Incident report", expanded=False):
        st.code(view.report_markdown, language="markdown")
    st.download_button(
        "Download report (.md)",
        view.report_markdown,
        file_name=f"{view.incident_id}.md",
        mime="text/markdown",
    )
