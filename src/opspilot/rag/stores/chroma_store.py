"""Chroma: local persistent dense search + rank_bm25 keyword index + RRF hybrid."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from opspilot.rag.bm25 import BM25Index
from opspilot.rag.embeddings import Vector
from opspilot.rag.models import Chunk, ChunkerName, Filters, SearchHit
from opspilot.rag.stores.base import check_lengths, rrf_hybrid

BATCH = 500


def _flags(prefix: str, values: Sequence[str]) -> dict[str, bool]:
    return {f"{prefix}__{v}": True for v in values}


def _any_of(prefix: str, values: Sequence[str]) -> dict[str, Any]:
    clauses: list[dict[str, Any]] = [{f"{prefix}__{v}": True} for v in values]
    return clauses[0] if len(clauses) == 1 else {"$or": clauses}


def chroma_where(filters: Filters | None) -> dict[str, Any] | None:
    """Translate Filters into a Chroma ``where`` clause (lists use boolean flag keys)."""
    if filters is None:
        return None
    clauses: list[dict[str, Any]] = []
    if filters.doc_types:
        clauses.append({"doc_type": {"$in": list(filters.doc_types)}})
    if filters.categories:
        clauses.append(_any_of("cat", filters.categories))
    if filters.services:
        clauses.append(_any_of("svc", filters.services))
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


class ChromaStore:
    """Chroma PersistentClient collection per chunker, plus a persisted BM25 index."""

    name = "chroma"

    def __init__(
        self,
        path: Path,
        chunker: ChunkerName = "markdown_section",
        collection: str | None = None,
    ) -> None:
        import chromadb  # heavy import, done lazily
        from chromadb.config import Settings

        self._path = path
        self._collection_name = collection or f"opspilot-kb-{chunker.replace('_', '-')}"
        self._bm25_path = path / f"bm25-{self._collection_name}.json"
        self._client = chromadb.PersistentClient(
            path=str(path), settings=Settings(anonymized_telemetry=False)
        )
        self._collection = self._open()
        self._bm25 = BM25Index.load(self._bm25_path)

    def _open(self) -> Any:
        return self._client.get_or_create_collection(
            self._collection_name,
            embedding_function=None,
            configuration={"hnsw": {"space": "cosine"}},
        )

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Vector]) -> None:
        check_lengths(chunks, vectors)
        for start in range(0, len(chunks), BATCH):
            batch = chunks[start : start + BATCH]
            self._collection.upsert(
                ids=[c.chunk_id for c in batch],
                embeddings=[list(v) for v in vectors[start : start + BATCH]],
                documents=[c.text for c in batch],
                metadatas=[self._metadata(c) for c in batch],
            )
        self._bm25.add(chunks)
        self._bm25.save(self._bm25_path)

    @staticmethod
    def _metadata(chunk: Chunk) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "doc_id": chunk.doc_id,
            "doc_type": chunk.doc_type,
            "title": chunk.title,
            "section": chunk.section,
            "index": chunk.index,
            **_flags("cat", chunk.categories),
            **_flags("svc", chunk.services),
        }
        if chunk.categories:
            meta["categories"] = list(chunk.categories)
        if chunk.services:
            meta["services"] = list(chunk.services)
        return meta

    def dense_search(
        self, vector: Vector, k: int, filters: Filters | None = None
    ) -> list[SearchHit]:
        if self.count() == 0:
            return []
        result = self._collection.query(
            query_embeddings=[list(vector)],
            n_results=min(k, self.count()),
            where=chroma_where(filters),
            include=["documents", "metadatas", "distances"],
        )
        hits = []
        for cid, text, meta, distance in zip(
            result["ids"][0],
            result["documents"][0],
            result["metadatas"][0],
            result["distances"][0],
            strict=True,
        ):
            chunk = Chunk(
                chunk_id=cid,
                doc_id=meta["doc_id"],
                doc_type=meta["doc_type"],
                title=meta["title"],
                section=meta["section"],
                index=meta["index"],
                text=text,
                categories=list(meta.get("categories") or []),
                services=list(meta.get("services") or []),
            )
            hits.append(SearchHit(chunk=chunk, score=1.0 - float(distance)))
        return hits

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
        dense = self.dense_search(vector, k, filters)
        keyword = self.keyword_search(text, k, filters)
        return rrf_hybrid(dense, keyword, k, alpha)

    def count(self) -> int:
        return int(self._collection.count())

    def reset(self) -> None:
        if self._collection_name in [c.name for c in self._client.list_collections()]:
            self._client.delete_collection(self._collection_name)
        self._collection = self._open()
        self._bm25 = BM25Index()
        self._bm25_path.unlink(missing_ok=True)
