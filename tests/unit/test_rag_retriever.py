from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from opspilot.rag.embeddings import EmbeddingCache, FastEmbedEmbedder, Vector
from opspilot.rag.models import Chunk, Filters, SearchHit
from opspilot.rag.rerank import NoopReranker
from opspilot.rag.retriever import Retriever, cite, dedupe_and_cap


def chunk(cid: str, doc: str, section: str = "") -> Chunk:
    return Chunk(
        chunk_id=cid,
        doc_id=doc,
        doc_type="runbook",
        title=doc,
        section=section,
        index=0,
        text=f"text of {cid}",
    )


class FakeEmbedder:
    dimension = 2

    def embed_passages(self, texts: Sequence[str]) -> list[Vector]:
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text: str) -> Vector:
        return [1.0, 0.0]


class FakeStore:
    name = "fake"

    def __init__(self, results: dict[str, list[SearchHit]]) -> None:
        self.results = results
        self.calls: list[tuple[str, str]] = []

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Vector]) -> None: ...

    def dense_search(
        self, vector: Vector, k: int, filters: Filters | None = None
    ) -> list[SearchHit]:
        self.calls.append(("dense", ""))
        return self.results["dense"][:k]

    def keyword_search(self, text: str, k: int, filters: Filters | None = None) -> list[SearchHit]:
        self.calls.append(("keyword", text))
        return self.results.get(text, self.results["keyword"])[:k]

    def hybrid_search(
        self, text: str, vector: Vector, k: int, alpha: float = 0.5, filters: Filters | None = None
    ) -> list[SearchHit]:
        self.calls.append(("hybrid", text))
        return self.results["hybrid"][:k]

    def count(self) -> int:
        return 0

    def reset(self) -> None: ...


class ReverseReranker:
    name = "reverse"

    def __init__(self) -> None:
        self.queries: list[str] = []

    def rerank(self, query: str, hits: Sequence[SearchHit]) -> list[SearchHit]:
        self.queries.append(query)
        return [SearchHit(chunk=h.chunk, score=float(i)) for i, h in enumerate(reversed(hits))]


def hits(*specs: tuple[str, str, str]) -> list[SearchHit]:
    return [
        SearchHit(chunk=chunk(c, d, s), score=1.0 / (i + 1)) for i, (c, d, s) in enumerate(specs)
    ]


def test_dedupe_and_cap() -> None:
    result = dedupe_and_cap(
        hits(
            ("1", "A", "s1"), ("2", "A", "s1"), ("3", "A", "s2"), ("4", "A", "s3"), ("5", "B", "s1")
        )
    )
    assert [h.chunk.chunk_id for h in result] == ["1", "3", "5"]


def test_cite_assigns_ranks_and_citations() -> None:
    cited = cite(hits(("1", "A", ""), ("2", "B", "")))
    assert [(c.rank, c.citation_id) for c in cited] == [(1, "R1"), (2, "R2")]


def test_retrieve_single_query_hybrid_with_rerank() -> None:
    store = FakeStore({"hybrid": hits(("1", "A", ""), ("2", "B", ""), ("3", "C", ""))})
    reranker = ReverseReranker()
    retriever = Retriever(store, FakeEmbedder(), reranker)
    result = retriever.retrieve("oom", k=2, rerank=True)
    assert [c.chunk_id for c in result.chunks] == ["3", "2"]
    assert reranker.queries == ["oom"]
    assert {"embed", "search", "rerank", "total"} <= set(result.timings_ms)


def test_retrieve_multi_query_fuses_with_rrf_and_reranks_on_first_query() -> None:
    store = FakeStore(
        {
            "q1": hits(("a", "A", ""), ("b", "B", "")),
            "q2": hits(("b", "B", ""), ("c", "C", "")),
            "keyword": [],
        }
    )
    reranker = ReverseReranker()
    result = Retriever(store, FakeEmbedder(), reranker).retrieve(
        ["q1", "q2"], k=3, mode="keyword", rerank=False
    )
    assert [c.chunk_id for c in result.chunks] == ["b", "a", "c"]
    assert reranker.queries == []
    assert "embed" not in result.timings_ms


def test_retrieve_dense_without_rerank_uses_noop() -> None:
    store = FakeStore({"dense": hits(("1", "A", ""), ("2", "B", ""))})
    retriever = Retriever(store, FakeEmbedder())
    assert isinstance(retriever.reranker, NoopReranker)
    result = retriever.retrieve("q", mode="dense", rerank=False)
    assert [c.chunk_id for c in result.chunks] == ["1", "2"]
    assert store.calls == [("dense", "")]


def test_retrieve_requires_a_query() -> None:
    with pytest.raises(ValueError, match="at least one query"):
        Retriever(FakeStore({}), FakeEmbedder()).retrieve([])


def test_embedding_cache_roundtrip(tmp_path: Path) -> None:
    cache = EmbeddingCache(tmp_path / "e.sqlite", "model-a")
    key = cache.key("hello")
    assert key != EmbeddingCache(tmp_path / "f.sqlite", "model-b").key("hello")
    cache.put_many({key: [0.5, 0.25]})
    assert cache.get_many([key, "missing"]) == {key: [0.5, 0.25]}
    assert len(cache) == 1


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def passage_embed(self, texts: list[str], batch_size: int) -> list[np.ndarray]:
        self.calls.append(texts)
        return [np.array([float(len(t)), 1.0], dtype=np.float32) for t in texts]


def test_fastembed_embedder_sorts_by_length_and_uses_cache(tmp_path: Path) -> None:
    embedder = FastEmbedEmbedder.__new__(FastEmbedEmbedder)
    model = _FakeModel()
    embedder._model = model  # type: ignore[assignment]
    embedder._cache = EmbeddingCache(tmp_path / "e.sqlite", "fake")
    texts = ["ccc", "a", "bb"]
    vectors = embedder.embed_passages(texts)
    assert [v[0] for v in vectors] == [3.0, 1.0, 2.0]  # original order restored
    assert model.calls == [["a", "bb", "ccc"]]  # embedded shortest first
    assert embedder.embed_passages(texts) == vectors
    assert len(model.calls) == 1  # second call served from cache
