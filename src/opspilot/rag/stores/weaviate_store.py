"""Weaviate: self-provided vectors, native BM25 and native hybrid search."""

import uuid
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

from opspilot.rag.embeddings import Vector
from opspilot.rag.models import Chunk, ChunkerName, Filters, SearchHit
from opspilot.rag.stores.base import check_lengths

BASE_COLLECTION = "KnowledgeChunk"
UUID_NAMESPACE = uuid.UUID("5b2f8e3c-6f1d-4c55-9a0e-0b5d1c6a7e21")
PROPERTIES = ("chunk_id", "doc_id", "doc_type", "title", "section", "index", "text")


def collection_name(chunker: ChunkerName) -> str:
    """``KnowledgeChunk`` for the default chunker, ``KnowledgeChunk_fixed`` for the baseline."""
    return BASE_COLLECTION if chunker == "markdown_section" else f"{BASE_COLLECTION}_{chunker}"


class WeaviateStore:
    """Weaviate collection per chunker; BM25 runs over the chunk ``text`` property."""

    name = "weaviate"

    def __init__(
        self,
        url: str,
        grpc_port: int = 50051,
        chunker: ChunkerName = "markdown_section",
        collection: str | None = None,
    ) -> None:
        import weaviate  # heavy import, done lazily
        from weaviate.classes.init import AdditionalConfig, Timeout

        parsed = urlparse(url)
        self._client = weaviate.connect_to_local(
            host=parsed.hostname or "localhost",
            port=parsed.port or 8080,
            grpc_port=grpc_port,
            # A generous init timeout: on a busy 8 GB laptop the gRPC health check can
            # take longer than the 2 s default.
            additional_config=AdditionalConfig(timeout=Timeout(init=30)),
        )
        self._name = collection or collection_name(chunker)
        self._collection = self._ensure()

    def close(self) -> None:
        self._client.close()

    def _ensure(self) -> Any:
        from weaviate.classes.config import Configure, DataType, Property, Tokenization

        if not self._client.collections.exists(self._name):
            text = DataType.TEXT
            self._client.collections.create(
                self._name,
                vector_config=Configure.Vectors.self_provided(),
                properties=[
                    Property(
                        name="chunk_id",
                        data_type=text,
                        skip_vectorization=True,
                        tokenization=Tokenization.FIELD,
                        index_searchable=False,
                    ),
                    Property(
                        name="doc_id",
                        data_type=text,
                        tokenization=Tokenization.FIELD,
                        index_searchable=False,
                    ),
                    Property(
                        name="doc_type",
                        data_type=text,
                        tokenization=Tokenization.FIELD,
                        index_searchable=False,
                    ),
                    Property(name="title", data_type=text, index_searchable=False),
                    Property(name="section", data_type=text, index_searchable=False),
                    Property(name="index", data_type=DataType.INT),
                    Property(name="text", data_type=text, tokenization=Tokenization.WORD),
                    Property(
                        name="categories",
                        data_type=DataType.TEXT_ARRAY,
                        tokenization=Tokenization.FIELD,
                        index_searchable=False,
                    ),
                    Property(
                        name="services",
                        data_type=DataType.TEXT_ARRAY,
                        tokenization=Tokenization.FIELD,
                        index_searchable=False,
                    ),
                ],
            )
        return self._client.collections.get(self._name)

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Vector]) -> None:
        check_lengths(chunks, vectors)
        with self._collection.batch.fixed_size(batch_size=200) as batch:
            for chunk, vector in zip(chunks, vectors, strict=True):
                batch.add_object(
                    properties={
                        **chunk.model_dump(include=set(PROPERTIES)),
                        "categories": list(chunk.categories),
                        "services": list(chunk.services),
                    },
                    vector=list(vector),
                    uuid=uuid.uuid5(UUID_NAMESPACE, chunk.chunk_id),
                )
        failed = self._collection.batch.failed_objects
        if failed:
            raise RuntimeError(f"{len(failed)} objects failed to import: {failed[0].message}")

    def _filters(self, filters: Filters | None) -> Any:
        from weaviate.classes.query import Filter

        if filters is None:
            return None
        parts = []
        if filters.doc_types:
            parts.append(Filter.by_property("doc_type").contains_any(list(filters.doc_types)))
        if filters.categories:
            parts.append(Filter.by_property("categories").contains_any(filters.categories))
        if filters.services:
            parts.append(Filter.by_property("services").contains_any(filters.services))
        return Filter.all_of(parts) if parts else None

    @staticmethod
    def _hits(objects: Sequence[Any], score: str) -> list[SearchHit]:
        hits = []
        for obj in objects:
            p = obj.properties
            value = (
                1.0 - float(obj.metadata.distance)
                if score == "distance"
                else float(obj.metadata.score or 0.0)
            )
            chunk = Chunk(
                chunk_id=p["chunk_id"],
                doc_id=p["doc_id"],
                doc_type=p["doc_type"],
                title=p["title"],
                section=p["section"],
                index=int(p["index"]),
                text=p["text"],
                categories=list(p.get("categories") or []),
                services=list(p.get("services") or []),
            )
            hits.append(SearchHit(chunk=chunk, score=value))
        return hits

    def dense_search(
        self, vector: Vector, k: int, filters: Filters | None = None
    ) -> list[SearchHit]:
        from weaviate.classes.query import MetadataQuery

        result = self._collection.query.near_vector(
            near_vector=list(vector),
            limit=k,
            filters=self._filters(filters),
            return_metadata=MetadataQuery(distance=True),
        )
        return self._hits(result.objects, "distance")

    def keyword_search(self, text: str, k: int, filters: Filters | None = None) -> list[SearchHit]:
        from weaviate.classes.query import MetadataQuery

        result = self._collection.query.bm25(
            query=text,
            query_properties=["text"],
            limit=k,
            filters=self._filters(filters),
            return_metadata=MetadataQuery(score=True),
        )
        return self._hits(result.objects, "score")

    def hybrid_search(
        self,
        text: str,
        vector: Vector,
        k: int,
        alpha: float = 0.5,
        filters: Filters | None = None,
    ) -> list[SearchHit]:
        from weaviate.classes.query import HybridFusion, MetadataQuery

        result = self._collection.query.hybrid(
            query=text,
            vector=list(vector),
            alpha=alpha,
            query_properties=["text"],
            fusion_type=HybridFusion.RELATIVE_SCORE,
            limit=k,
            filters=self._filters(filters),
            return_metadata=MetadataQuery(score=True),
        )
        return self._hits(result.objects, "score")

    def count(self) -> int:
        result = self._collection.aggregate.over_all(total_count=True)
        return int(result.total_count or 0)

    def reset(self) -> None:
        if self._client.collections.exists(self._name):
            self._client.collections.delete(self._name)
        self._collection = self._ensure()
