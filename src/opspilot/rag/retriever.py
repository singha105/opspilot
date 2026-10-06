"""The retriever: candidate search, multi-query fusion, reranking, dedupe and citations."""

import time
from collections.abc import Sequence

from opspilot.rag.embeddings import Embedder
from opspilot.rag.fusion import rrf
from opspilot.rag.models import Filters, RetrievalResult, RetrievedChunk, SearchHit, SearchMode
from opspilot.rag.rerank import NoopReranker, Reranker
from opspilot.rag.stores.base import VectorStore

CANDIDATES = 20
MAX_PER_DOC = 2


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def dedupe_and_cap(hits: Sequence[SearchHit], max_per_doc: int = MAX_PER_DOC) -> list[SearchHit]:
    """Drop near-duplicates (same doc and section) and keep at most N chunks per document."""
    seen_sections: set[tuple[str, str]] = set()
    per_doc: dict[str, int] = {}
    kept: list[SearchHit] = []
    for hit in hits:
        key = (hit.chunk.doc_id, hit.chunk.section)
        if key in seen_sections or per_doc.get(hit.chunk.doc_id, 0) >= max_per_doc:
            continue
        seen_sections.add(key)
        per_doc[hit.chunk.doc_id] = per_doc.get(hit.chunk.doc_id, 0) + 1
        kept.append(hit)
    return kept


def cite(hits: Sequence[SearchHit]) -> list[RetrievedChunk]:
    """Turn hits into ranked chunks with citation ids R1..Rk."""
    return [
        RetrievedChunk(
            chunk_id=h.chunk.chunk_id,
            doc_id=h.chunk.doc_id,
            doc_type=h.chunk.doc_type,
            title=h.chunk.title,
            section=h.chunk.section,
            text=h.chunk.text,
            score=round(h.score, 6),
            rank=i,
            citation_id=f"R{i}",
        )
        for i, h in enumerate(hits, start=1)
    ]


class Retriever:
    """Retrieves cited chunks from one store."""

    def __init__(
        self,
        store: VectorStore,
        embedder: Embedder,
        reranker: Reranker | None = None,
        *,
        candidates: int = CANDIDATES,
        alpha: float = 0.5,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.reranker = reranker or NoopReranker()
        self.candidates = candidates
        self.alpha = alpha

    def _search(
        self, query: str, mode: SearchMode, filters: Filters | None, timings: dict[str, float]
    ) -> list[SearchHit]:
        if mode == "keyword":
            start = time.perf_counter()
            hits = self.store.keyword_search(query, self.candidates, filters)
            timings["search"] = timings.get("search", 0.0) + _ms(start)
            return hits
        start = time.perf_counter()
        vector = self.embedder.embed_query(query)
        timings["embed"] = timings.get("embed", 0.0) + _ms(start)
        start = time.perf_counter()
        if mode == "dense":
            hits = self.store.dense_search(vector, self.candidates, filters)
        else:
            hits = self.store.hybrid_search(query, vector, self.candidates, self.alpha, filters)
        timings["search"] = timings.get("search", 0.0) + _ms(start)
        return hits

    def retrieve(
        self,
        query: str | Sequence[str],
        k: int = 6,
        mode: SearchMode = "hybrid",
        rerank: bool = True,
        filters: Filters | None = None,
    ) -> RetrievalResult:
        """Return the top ``k`` cited chunks for one query or several query variants.

        Several queries are fused with RRF; reranking always scores against the first
        (original) query.
        """
        queries = [query] if isinstance(query, str) else list(query)
        if not queries:
            raise ValueError("at least one query is required")
        timings: dict[str, float] = {}
        total = time.perf_counter()

        results = [self._search(q, mode, filters, timings) for q in queries]
        if len(results) == 1:
            candidates = results[0][: self.candidates]
        else:
            by_id = {h.chunk.chunk_id: h.chunk for hits in results for h in hits}
            fused = rrf([[h.chunk.chunk_id for h in hits] for hits in results])
            candidates = [
                SearchHit(chunk=by_id[cid], score=score) for cid, score in fused[: self.candidates]
            ]

        if rerank:
            start = time.perf_counter()
            candidates = self.reranker.rerank(queries[0], candidates)
            timings["rerank"] = _ms(start)

        chunks = cite(dedupe_and_cap(candidates)[:k])
        timings["total"] = _ms(total)
        return RetrievalResult(chunks=chunks, timings_ms=timings)
