"""Formatting helpers shared by nodes: compact, untrusted-wrapped prompt sections."""

import json
from collections.abc import Sequence

from opspilot.agent.prompts import Prompt, load_prompt, untrusted
from opspilot.models import Alert, RootCauseCategory
from opspilot.models.incident import Evidence
from opspilot.rag.models import RetrievedChunk

CHUNK_CHARS = 700
CATEGORIES = ", ".join(c.value for c in RootCauseCategory)


def alert_block(alert: Alert) -> str:
    return untrusted("alert", alert.model_dump_json(exclude_defaults=True))


def evidence_block(evidence: Sequence[Evidence], *, excerpts: bool = True) -> str:
    if not evidence:
        return "(no evidence yet)"
    lines = []
    for e in evidence:
        lines.append(f"[{e.id}] {e.tool}({_args(e.args)}): {e.summary}")
        if excerpts and e.excerpt:
            lines.extend("    " + line for line in e.excerpt.splitlines())
    return untrusted("evidence", "\n".join(lines))


def chunks_block(chunks: Sequence[RetrievedChunk]) -> str:
    if not chunks:
        return "(no runbooks retrieved)"
    parts = [
        f"[{c.citation_id}] {c.doc_id}: {c.title} > {c.section or 'overview'}\n"
        f"{c.text[:CHUNK_CHARS]}"
        for c in chunks
    ]
    return untrusted("runbooks", "\n\n".join(parts))


def compact_json(model: object) -> str:
    dump = getattr(model, "model_dump", None)
    data = dump(mode="json", exclude_none=True) if callable(dump) else model
    return json.dumps(data, separators=(",", ":"))


def _args(args: dict[str, object]) -> str:
    return ", ".join(f"{k}={v}" for k, v in args.items() if v not in (None, False, ""))


def prompt(name: str) -> Prompt:
    return load_prompt(name)
