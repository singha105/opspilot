"""Local BM25 keyword index (rank_bm25) persisted as JSON.

Used by stores without native keyword search (Chroma, Pinecone). Tokenization
mirrors Weaviate's default ``word`` tokenizer: lowercase, split on anything that is
not a letter or digit, and drop common English stopwords.
"""

import json
import re
from collections.abc import Sequence
from pathlib import Path

from rank_bm25 import BM25Okapi

from opspilot.rag.models import Chunk, Filters, SearchHit

_TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "if",
        "in",
        "into",
        "is",
        "it",
        "no",
        "not",
        "of",
        "on",
        "or",
        "such",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "was",
        "will",
        "with",
    ]
)


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens without stopwords."""
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS]


class BM25Index:
    """In-memory BM25 over chunks, with JSON persistence."""

    def __init__(self, chunks: Sequence[Chunk] = ()) -> None:
        self._chunks: list[Chunk] = list(chunks)
        self._bm25: BM25Okapi | None = None
        self._rebuild()

    def _rebuild(self) -> None:
        corpus = [tokenize(c.text) for c in self._chunks]
        self._bm25 = BM25Okapi(corpus) if corpus else None

    def add(self, chunks: Sequence[Chunk]) -> None:
        by_id = {c.chunk_id: c for c in self._chunks}
        by_id.update({c.chunk_id: c for c in chunks})
        self._chunks = sorted(by_id.values(), key=lambda c: c.chunk_id)
        self._rebuild()

    def __len__(self) -> int:
        return len(self._chunks)

    def search(self, query: str, k: int, filters: Filters | None = None) -> list[SearchHit]:
        """Top-``k`` chunks by BM25 score (zero-score chunks are dropped)."""
        if self._bm25 is None:
            return []
        scores = self._bm25.get_scores(tokenize(query))
        order = sorted(range(len(self._chunks)), key=lambda i: (-scores[i], i))
        hits: list[SearchHit] = []
        for i in order:
            if scores[i] <= 0 or len(hits) >= k:
                break
            chunk = self._chunks[i]
            if filters is None or filters.matches(chunk):
                hits.append(SearchHit(chunk=chunk, score=float(scores[i])))
        return hits

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([c.model_dump() for c in self._chunks]))

    @classmethod
    def load(cls, path: Path) -> "BM25Index":
        if not path.is_file():
            return cls()
        return cls([Chunk.model_validate(c) for c in json.loads(path.read_text())])
