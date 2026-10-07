import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from opspilot.mcp_servers.common import AuditLog, ToolRunner
from opspilot.mcp_servers.knowledge.server import DESCRIPTIONS, KbTools, build_server
from opspilot.rag.models import Chunk, Filters, SearchHit
from opspilot.rag.retriever import Retriever

KNOWLEDGE = Path(__file__).parents[2] / "knowledge"


class StubStore:
    name = "stub"

    def __init__(self) -> None:
        self.filters: list[Filters | None] = []

    def hybrid_search(
        self,
        text: str,
        vector: list[float],
        k: int,
        alpha: float = 0.5,
        filters: Filters | None = None,
    ) -> list[SearchHit]:
        self.filters.append(filters)
        chunk = Chunk(
            chunk_id="c1",
            doc_id="rb-oom-killed",
            doc_type="runbook",
            title="OOM",
            section="Symptoms",
            index=0,
            text="exit code 137 token=abc123secret",
        )
        return [SearchHit(chunk=chunk, score=0.9)]

    def __getattr__(self, name: str) -> Any:  # other protocol methods are unused here
        raise AssertionError(name)


class StubEmbedder:
    dimension = 2

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]


@pytest.fixture
def store() -> StubStore:
    return StubStore()


@pytest.fixture
def kb(store: StubStore, tmp_path: Path) -> KbTools:
    runner = ToolRunner(AuditLog(tmp_path / "audit.jsonl", "opspilot-kb", "live"))
    return KbTools(lambda: Retriever(store, StubEmbedder()), KNOWLEDGE, runner)  # type: ignore[arg-type]


def test_search_returns_citation_ready_chunks(kb: KbTools, store: StubStore) -> None:
    out = json.loads(kb.search_knowledge("pods restart exit 137", k=3, doc_types=["runbook"]))
    chunk = out["chunks"][0]
    assert set(chunk) == {"citation_id", "doc_id", "doc_type", "title", "section", "text", "score"}
    assert chunk["citation_id"] == "R1"
    assert "abc123secret" not in chunk["text"]
    assert store.filters[-1] == Filters(doc_types=["runbook"])


def test_search_rejects_unknown_category(kb: KbTools) -> None:
    out = json.loads(kb.search_knowledge("x y z", categories=["NOT_REAL"]))
    assert out["error"]["type"] == "invalid_argument"


def test_get_document_whole_and_section(kb: KbTools) -> None:
    whole = json.loads(kb.get_document("rb-oom-killed"))
    assert whole["title"] == "Container OOMKilled / exit code 137"
    assert "Remediation options" in whole["sections"]
    section = json.loads(kb.get_document("rb-oom-killed", section="remediation"))
    assert section["text"].startswith("- **Config regression:**")
    assert json.loads(kb.get_document("rb-nope"))["error"]["type"] == "not_found"
    bad = json.loads(kb.get_document("rb-oom-killed", section="nonexistent"))
    assert "Sections:" in bad["error"]["hint"]


def test_long_document_is_truncated_with_hint(kb: KbTools) -> None:
    out = json.loads(kb.get_document("k8s-deployment"))
    assert out["truncated"] is True
    assert "section" in out["hint"]


def test_kb_server_catalog() -> None:
    tools = KbTools(lambda: None, KNOWLEDGE, ToolRunner(AuditLog(Path("/dev/null"), "kb", "live")))  # type: ignore[arg-type, return-value]
    listed = asyncio.run(build_server(tools).list_tools())
    assert {t.name for t in listed} == set(DESCRIPTIONS) == {"search_knowledge", "get_document"}
    assert all(len((t.description or "").split()) <= 80 for t in listed)


def test_kb_bounds_are_enforced_in_the_audited_path(kb: KbTools) -> None:
    assert json.loads(kb.search_knowledge("pods", k=50))["error"]["type"] == "invalid_argument"
    assert json.loads(kb.search_knowledge("x"))["error"]["type"] == "invalid_argument"
