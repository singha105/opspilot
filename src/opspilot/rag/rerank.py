"""Rerankers: a FlashRank cross-encoder and a no-op baseline."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from opspilot.rag.models import SearchHit


class Reranker(Protocol):
    """Reorders candidate hits for a query; returned scores replace store scores."""

    name: str

    def rerank(self, query: str, hits: Sequence[SearchHit]) -> list[SearchHit]: ...


class NoopReranker:
    """Keeps the incoming order and scores."""

    name = "none"

    def rerank(self, query: str, hits: Sequence[SearchHit]) -> list[SearchHit]:
        return list(hits)


class FlashRankReranker:
    """FlashRank MiniLM cross-encoder (ONNX, runs on CPU, about 30 MB)."""

    name = "flashrank"

    def __init__(self, model: str = "ms-marco-MiniLM-L-12-v2", cache_dir: Path | None = None):
        from flashrank import Ranker  # heavy import, done lazily

        self.model = model
        kwargs: dict[str, Any] = {"model_name": model, "log_level": "WARNING"}
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)
            kwargs["cache_dir"] = str(cache_dir)
        self._ranker = Ranker(**kwargs)

    def rerank(self, query: str, hits: Sequence[SearchHit]) -> list[SearchHit]:
        from flashrank import RerankRequest

        if not hits:
            return []
        passages = [{"id": str(i), "text": h.chunk.text} for i, h in enumerate(hits)]
        ranked = self._ranker.rerank(RerankRequest(query=query, passages=passages))
        return [SearchHit(chunk=hits[int(r["id"])].chunk, score=float(r["score"])) for r in ranked]
