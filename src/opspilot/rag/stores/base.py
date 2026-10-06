"""The VectorStore contract every backend implements."""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from opspilot.rag.embeddings import Vector
from opspilot.rag.fusion import RRF_K, rrf
from opspilot.rag.models import Chunk, Filters, SearchHit


@runtime_checkable
class VectorStore(Protocol):
    """Dense, keyword and hybrid search over chunks. Scores: higher is better."""

    name: str

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Vector]) -> None: ...

    def dense_search(
        self, vector: Vector, k: int, filters: Filters | None = None
    ) -> list[SearchHit]: ...

    def keyword_search(
        self, text: str, k: int, filters: Filters | None = None
    ) -> list[SearchHit]: ...

    def hybrid_search(
        self,
        text: str,
        vector: Vector,
        k: int,
        alpha: float = 0.5,
        filters: Filters | None = None,
    ) -> list[SearchHit]: ...

    def count(self) -> int: ...

    def reset(self) -> None: ...


def rrf_hybrid(
    dense: list[SearchHit], keyword: list[SearchHit], k: int, alpha: float
) -> list[SearchHit]:
    """Weighted RRF of dense and keyword hits; ``alpha`` weights dense, ``1 - alpha`` keyword.

    With ``alpha = 0.5`` this is standard RRF (scaled by 0.5), matching the fusion
    used for stores that have no native hybrid search.
    """
    chunks = {h.chunk.chunk_id: h.chunk for h in [*dense, *keyword]}
    fused = rrf(
        [[h.chunk.chunk_id for h in dense], [h.chunk.chunk_id for h in keyword]],
        k=RRF_K,
        weights=[alpha, 1.0 - alpha],
    )
    return [SearchHit(chunk=chunks[cid], score=score) for cid, score in fused[:k]]


def check_lengths(chunks: Sequence[Chunk], vectors: Sequence[Vector]) -> None:
    if len(chunks) != len(vectors):
        raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
