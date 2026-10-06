"""Wiring shared by the CLI and the eval runner: ingest and retriever construction."""

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

from opspilot.config import Settings, get_settings
from opspilot.logging import get_logger
from opspilot.rag.chunking import chunk_documents
from opspilot.rag.embeddings import FastEmbedEmbedder
from opspilot.rag.loader import load_documents
from opspilot.rag.models import ChunkerName
from opspilot.rag.rerank import FlashRankReranker, NoopReranker, Reranker
from opspilot.rag.retriever import Retriever
from opspilot.rag.stores import StoreName, get_store

log = get_logger(__name__)


@dataclass(frozen=True)
class IngestReport:
    store: str
    chunker: str
    documents: int
    chunks: int
    stored: int


@lru_cache(maxsize=1)
def default_embedder() -> FastEmbedEmbedder:
    """The process-wide embedder (model loads once)."""
    settings = get_settings()
    return FastEmbedEmbedder(
        settings.embedding_model,
        cache_path=settings.embedding_cache,
        model_dir=settings.model_dir,
    )


@lru_cache(maxsize=1)
def default_reranker() -> FlashRankReranker:
    """The process-wide FlashRank reranker (model loads once)."""
    settings = get_settings()
    return FlashRankReranker(settings.reranker_model, cache_dir=settings.model_dir / "flashrank")


def ingest(
    stores: Sequence[StoreName],
    chunkers: Sequence[ChunkerName],
    settings: Settings | None = None,
) -> list[IngestReport]:
    """Reset and fully re-index each (store, chunker) pair. Safe to re-run."""
    settings = settings or get_settings()
    docs = load_documents(settings.knowledge_dir)
    embedder = default_embedder()
    reports = []
    for chunker in chunkers:
        chunks = chunk_documents(docs, chunker)
        vectors = embedder.embed_passages([c.text for c in chunks])
        for name in stores:
            store = get_store(name, chunker, dimension=embedder.dimension, settings=settings)
            store.reset()
            store.upsert(chunks, vectors)
            report = IngestReport(name, chunker, len(docs), len(chunks), store.count())
            log.info("ingested", **report.__dict__)
            reports.append(report)
            _close(store)
    return reports


def build_retriever(
    store: StoreName | None = None,
    chunker: ChunkerName = "markdown_section",
    *,
    rerank: bool | None = None,
    alpha: float | None = None,
    settings: Settings | None = None,
) -> Retriever:
    """A retriever over one store/chunker collection; unset options use the ADR-0004 defaults."""
    settings = settings or get_settings()
    store = store or settings.default_store
    rerank = settings.retrieval_rerank if rerank is None else rerank
    alpha = settings.retrieval_alpha if alpha is None else alpha
    embedder = default_embedder()
    reranker: Reranker = default_reranker() if rerank else NoopReranker()
    backend = get_store(store, chunker, dimension=embedder.dimension, settings=settings)
    return Retriever(backend, embedder, reranker, alpha=alpha)


def _close(store: object) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        close()
