from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opspilot.cli import app
from opspilot.config import get_settings
from opspilot.rag import cli as kb_cli
from opspilot.rag import pipeline
from opspilot.rag.embeddings import Vector
from opspilot.rag.models import Filters
from opspilot.rag.stores import StoreUnavailableError, available_stores, get_store
from opspilot.rag.stores.chroma_store import chroma_where
from opspilot.rag.stores.pinecone_store import pinecone_filter

runner = CliRunner()


class HashEmbedder:
    """Deterministic 8-d bag-of-letters vectors: no model download needed."""

    dimension = 8

    def _vec(self, text: str) -> Vector:
        counts = [0.0] * 8
        for ch in text.lower():
            if ch.isalpha():
                counts[ord(ch) % 8] += 1.0
        norm = sum(c * c for c in counts) ** 0.5 or 1.0
        return [c / norm for c in counts]

    def embed_passages(self, texts: Sequence[str]) -> list[Vector]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> Vector:
        return self._vec(text)


@pytest.fixture
def local_kb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("OPSPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("PINECONE_API_KEY", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(pipeline, "default_embedder", HashEmbedder)
    monkeypatch.setattr(kb_cli, "available_stores", lambda *a: ["chroma"])
    yield tmp_path
    get_settings.cache_clear()


def test_ingest_search_and_stats_with_chroma(local_kb: Path) -> None:
    result = runner.invoke(
        app, ["kb", "ingest", "--store", "chroma", "--chunker", "markdown_section"]
    )
    assert result.exit_code == 0, result.output
    assert "740" in result.output

    again = runner.invoke(
        app, ["kb", "ingest", "--store", "chroma", "--chunker", "markdown_section"]
    )
    assert again.exit_code == 0  # idempotent: same counts on re-run

    search = runner.invoke(
        app,
        [
            "kb",
            "search",
            "redis connection refused",
            "--store",
            "chroma",
            "--no-rerank",
            "--k",
            "3",
        ],
    )
    assert search.exit_code == 0, search.output
    assert "R1" in search.output
    assert "total=" in search.output

    stats = runner.invoke(app, ["kb", "stats"])
    assert stats.exit_code == 0, stats.output
    assert "runbook=22" in stats.output
    assert "not configured" in stats.output


def test_available_stores_and_pinecone_gate(
    local_kb: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert available_stores() == ["weaviate", "chroma"]
    with pytest.raises(StoreUnavailableError, match="PINECONE_API_KEY"):
        get_store("pinecone")
    monkeypatch.setenv("PINECONE_API_KEY", "pc-test")
    get_settings.cache_clear()
    assert "pinecone" in available_stores()


def test_filter_translation() -> None:
    assert chroma_where(None) is None
    assert chroma_where(Filters()) is None
    assert chroma_where(Filters(doc_types=["runbook"])) == {"doc_type": {"$in": ["runbook"]}}
    assert chroma_where(Filters(categories=["A", "B"], services=["redis"])) == {
        "$and": [{"$or": [{"cat__A": True}, {"cat__B": True}]}, {"svc__redis": True}]
    }
    assert pinecone_filter(None) is None
    assert pinecone_filter(Filters(services=["redis"])) == {"services": {"$in": ["redis"]}}
    assert pinecone_filter(Filters(doc_types=["runbook"], categories=["A"])) == {
        "$and": [{"doc_type": {"$in": ["runbook"]}}, {"categories": {"$in": ["A"]}}]
    }
