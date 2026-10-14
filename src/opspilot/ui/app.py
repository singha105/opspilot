"""OpsPilot operator console (Streamlit): ``streamlit run src/opspilot/ui/app.py``.

Every action goes through ``IncidentService``, the same layer the API and CLI use, so a run
started here can be approved from the API or the CLI and the other way round.
"""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import streamlit as st

from opspilot.config import Settings
from opspilot.models.incident import ApprovalDecision
from opspilot.service import IncidentService, ServiceError
from opspilot.ui.views import (
    StatusSink,
    clean_section,
    escape_md,
    render_incident,
    status_label,
)

REPORT = Path("evals/REPORT.md")
CHARTS = Path("evals/charts")
INCIDENT_KEY = "incident_id"
DEFAULT_SCENARIO = "oom-payments"

# Tests replace this to run the console against fakes.
service_factory: Callable[[], IncidentService] = IncidentService


@st.cache_resource
def _service() -> IncidentService:
    return service_factory()


def service() -> IncidentService:
    return _service()


def banner(settings: Settings) -> None:
    mode = settings.api_mode
    where = (
        "recorded cluster snapshots (no cluster needed)" if mode == "replay" else "the k3d cluster"
    )
    st.info(
        f"**{mode.upper()} mode**: investigating {where}. The agent's cluster access is "
        "**read-only**; a change happens only after you approve it, through a separate gated "
        "server with a single-use token.",
        icon="🔒",
    )


def _decide(incident_id: str) -> Callable[[str, str, str], None]:
    def decide(decision: str, approver: str, reason: str) -> None:
        choice = ApprovalDecision.model_validate(
            {"decision": decision, "approver": approver, "reason": reason}
        )
        with st.status("Resuming the incident…", expanded=True) as box:
            try:
                asyncio.run(service().decide(incident_id, choice, [StatusSink(box)]))
            except ServiceError as exc:
                box.update(label=str(exc), state="error")
                return
            box.update(label="Done", state="complete")
        st.rerun()

    return decide


# ---- pages ------------------------------------------------------------------------------------


def incidents_page() -> None:
    svc = service()
    st.title("Incidents")
    banner(svc.settings)
    alert, scenario = _choose_alert(svc)
    if st.button("Investigate", type="primary", disabled=alert is None):
        try:
            incident_id = svc.create(mode=svc.settings.api_mode, scenario=scenario, source="ui")
        except ServiceError as exc:
            st.error(str(exc))
            return
        st.session_state[INCIDENT_KEY] = incident_id
        with st.status("Investigating…", expanded=True) as box:
            asyncio.run(svc.run(incident_id, alert, [StatusSink(box)]))
            box.update(label="Investigation finished", state="complete", expanded=False)
    current = st.session_state.get(INCIDENT_KEY)
    if current:
        st.divider()
        render_incident(svc.get(current), _decide(current))


def _choose_alert(svc: IncidentService) -> tuple[Any, str | None]:
    """The alert to investigate (and its scenario in replay mode), or (None, None)."""
    if svc.settings.api_mode == "replay":
        scenarios = svc.scenarios()
        ids = list(scenarios)
        sid = st.selectbox(
            "Recorded scenario",
            ids,
            index=ids.index(DEFAULT_SCENARIO) if DEFAULT_SCENARIO in ids else 0,
            format_func=lambda s: f"{s} — {scenarios[s].title}",
        )
        alert, _ = svc.scenario_alert(sid)
        st.caption(f"Alert: {escape_md(alert['summary'])}")
        return alert, sid
    source = st.radio("Alert", ["Paste an alert", "Received by the webhook"], horizontal=True)
    if source == "Paste an alert":
        text = st.text_area("Alertmanager JSON, an OpsPilot alert, or plain text", height=140)
        if not text.strip():
            return None, None
        try:
            return json.loads(text), None
        except json.JSONDecodeError:
            return text, None
    received = [
        r
        for r in svc.list()
        if svc.meta(r.incident_id).source == "webhook" and r.mode == svc.settings.api_mode
    ]
    if not received:
        st.caption("No alerts received yet: POST them to /alerts on the API.")
        return None, None
    pick = st.selectbox(
        "Received incidents",
        [r.incident_id for r in received],
        format_func=lambda i: next(
            f"{r.incident_id} · {status_label(r.status)} · {r.alert}"
            for r in received
            if r.incident_id == i
        ),
    )
    if st.button("Open"):
        st.session_state[INCIDENT_KEY] = pick
    return None, None


def runs_page() -> None:
    svc = service()
    st.title("Runs")
    runs = svc.list()
    if not runs:
        st.caption("No runs yet.")
        return
    statuses = sorted({r.status for r in runs})
    modes = sorted({r.mode for r in runs})
    categories = sorted({r.category or "—" for r in runs})
    c1, c2, c3 = st.columns(3)
    want_status = c1.multiselect("Status", statuses, placeholder="All")
    want_mode = c2.multiselect("Mode", modes, placeholder="All")
    want_category = c3.multiselect("Category", categories, placeholder="All")
    shown = [
        r
        for r in runs
        if (not want_status or r.status in want_status)
        and (not want_mode or r.mode in want_mode)
        and (not want_category or (r.category or "—") in want_category)
    ]
    st.dataframe(
        [
            {
                "incident": r.incident_id,
                "status": status_label(r.status),
                "mode": r.mode,
                "category": r.category or "—",
                "confidence": r.confidence,
                "action": r.action or "—",
                "alert": r.alert,
                "updated": r.updated_at,
            }
            for r in shown
        ],
        hide_index=True,
        use_container_width=True,
    )
    pick = st.selectbox("Open a run", [r.incident_id for r in shown])
    if pick:
        st.divider()
        render_incident(svc.get(pick), _decide(pick))


def evals_page() -> None:
    from opspilot.evals.agent_report import read_block

    st.title("Evaluation")
    if not REPORT.exists():
        st.warning("evals/REPORT.md not found.")
        return
    text = REPORT.read_text()
    st.caption(
        "Held-out test split (20 injected faults), replayed from recordings with qwen3:4b on an "
        "8 GB laptop. Full report: evals/REPORT.md."
    )
    st.markdown(read_block(text, "headline"))
    st.subheader("Retrieval ablation (test split)", anchor=False)
    st.image(str(CHARTS / "accuracy-by-config.png"))
    st.markdown(read_block(text, "ablation"))
    st.subheader("By category (all 30 faults)", anchor=False)
    st.image(str(CHARTS / "accuracy-by-category.png"))
    st.subheader("Safety", anchor=False)
    st.markdown(read_block(text, "safety"))
    st.subheader("Live subset", anchor=False)
    st.markdown(read_block(text, "live"))


@st.cache_resource
def _retriever() -> Any:
    from opspilot.rag.pipeline import build_retriever

    return build_retriever(rerank=True)


PIPELINES = (
    ("Dense (vectors only)", "dense", False),
    ("Hybrid (BM25 + vectors)", "hybrid", False),
    ("Hybrid + rerank", "hybrid", True),
)


def knowledge_page() -> None:
    st.title("Knowledge")
    st.caption(
        "One query through three retrieval pipelines over the same index: dense vectors, "
        "hybrid BM25 + vectors (Weaviate), and hybrid with a cross-encoder reranker."
    )
    query = st.text_input(
        "Search the runbooks, postmortems and Kubernetes docs",
        placeholder="payments pods restart with exit code 137",
    )
    k = st.slider("Results per pipeline", 3, 8, 5)
    if not query.strip():
        return
    try:
        retriever = _retriever()
    except Exception as exc:  # the store is optional for the rest of the console
        st.error(
            f"Knowledge base unavailable ({type(exc).__name__}); run `make infra-up` and ingest."
        )
        return
    for column, (label, mode, rerank) in zip(st.columns(3), PIPELINES, strict=True):
        result = retriever.retrieve(query, k, mode=mode, rerank=rerank)
        with column:
            st.markdown(f"**{label}**")
            st.caption(f"{result.timings_ms.get('total', 0):.0f} ms")
            for rank, chunk in enumerate(result.chunks, start=1):
                with st.expander(f"{rank}. {chunk.title} — {clean_section(chunk.section)}"):
                    st.caption(f"`{chunk.doc_id}` ({chunk.doc_type}) · score {chunk.score:.3f}")
                    st.text(chunk.text)


def main() -> None:
    st.set_page_config(page_title="OpsPilot", page_icon="🛠️", layout="wide")
    page = st.navigation(
        [
            st.Page(incidents_page, title="Incidents", icon="🚨", default=True),
            st.Page(runs_page, title="Runs", icon="🗂️"),
            st.Page(evals_page, title="Evals", icon="📊"),
            st.Page(knowledge_page, title="Knowledge", icon="📚"),
        ]
    )
    page.run()


if __name__ == "__main__":
    main()
