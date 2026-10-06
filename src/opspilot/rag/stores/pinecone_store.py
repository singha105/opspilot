"""Pinecone (optional, free Starter tier): dense via Pinecone, keyword via local BM25, RRF.

Only constructed when ``PINECONE_API_KEY`` is set. Nothing in OpsPilot requires it.
"""

import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from opspilot.rag.bm25 import BM25Index
from opspilot.rag.embeddings import Vector
from opspilot.rag.models import Chunk, ChunkerName, Filters, SearchHit
from opspilot.rag.stores.base import check_lengths, rrf_hybrid

BATCH = 100


def pinecone_filter(filters: Filters | None) -> dict[str, Any] | None:
    """Translate Filters into Pinecone metadata filter syntax."""
    if filters is None:
        return None
    clauses: list[dict[str, Any]] = []
    if filters.doc_types:
        clauses.append({"doc_type": {"$in": list(filters.doc_types)}})
    if filters.categories:
        clauses.append({"categories": {"$in": list(filters.categories)}})
    if filters.services:
        clauses.append({"services": {"$in": list(filters.services)}})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


class PineconeStore:
    """Serverless Pinecone index per chunker plus a local BM25 index."""

    name = "pinecone"

    def __init__(
        self,
        api_key: str,
        dimension: int,
        local_dir: Path,
        chunker: ChunkerName = "markdown_section",
        cloud: str = "aws",
        region: str = "us-east-1",
        index_name: str | None = None,
    ) -> None:
        from pinecone import Pinecone, ServerlessSpec  # optional dependency

        self._pc = Pinecone(api_key=api_key)
        self._index_name = index_name or f"opspilot-kb-{chunker.replace('_', '-')}"
        self._dimension = dimension
        self._spec = ServerlessSpec(cloud=cloud, region=region)
        self._bm25_path = local_dir / f"bm25-{self._index_name}.json"
        self._bm25 = BM25Index.load(self._bm25_path)
        self._index = self._ensure()

    def _ensure(self) -> Any:
        if not self._pc.has_index(self._index_name):
            self._pc.create_index(
                name=self._index_name,
                dimension=self._dimension,
                metric="cosine",
                spec=self._spec,
            )
            while not self._pc.describe_index(self._index_name).status["ready"]:
                time.sleep(1)
        return self._pc.Index(self._index_name)

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Vector]) -> None:
        check_lengths(chunks, vectors)
        for start in range(0, len(chunks), BATCH):
            self._index.upsert(
                vectors=[
                    {
                        "id": c.chunk_id,
                        "values": list(v),
                        "metadata": c.model_dump(exclude={"chunk_id"}),
                    }
                    for c, v in zip(
                        chunks[start : start + BATCH], vectors[start : start + BATCH], strict=True
                    )
                ]
            )
        self._bm25.add(chunks)
        self._bm25.save(self._bm25_path)

    def dense_search(
        self, vector: Vector, k: int, filters: Filters | None = None
    ) -> list[SearchHit]:
        result = self._index.query(
            vector=list(vector),
            top_k=k,
            include_metadata=True,
            filter=pinecone_filter(filters),
        )
        return [
            SearchHit(
                chunk=Chunk(chunk_id=m["id"], **m["metadata"]),
                score=float(m["score"]),
            )
            for m in result["matches"]
        ]

    def keyword_search(self, text: str, k: int, filters: Filters | None = None) -> list[SearchHit]:
        return self._bm25.search(text, k, filters)

    def hybrid_search(
        self,
        text: str,
        vector: Vector,
        k: int,
        alpha: float = 0.5,
        filters: Filters | None = None,
    ) -> list[SearchHit]:
        return rrf_hybrid(
            self.dense_search(vector, k, filters), self.keyword_search(text, k, filters), k, alpha
        )

    def count(self) -> int:
        stats = self._index.describe_index_stats()
        return int(stats["total_vector_count"])

    def reset(self) -> None:
        if self._pc.has_index(self._index_name):
            self._pc.delete_index(self._index_name)
        self._index = self._ensure()
        self._bm25 = BM25Index()
        self._bm25_path.unlink(missing_ok=True)
