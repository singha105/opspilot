"""Vector store backends and the ``get_store`` factory."""

from typing import Literal

from opspilot.config import Settings, get_settings
from opspilot.rag.models import ChunkerName
from opspilot.rag.stores.base import VectorStore

StoreName = Literal["weaviate", "chroma", "pinecone"]
STORE_NAMES: tuple[StoreName, ...] = ("weaviate", "chroma", "pinecone")


class StoreUnavailableError(RuntimeError):
    """Raised when a store is requested but not configured (e.g. no Pinecone key)."""


def available_stores(settings: Settings | None = None) -> list[StoreName]:
    """Stores that can be used right now. Pinecone needs PINECONE_API_KEY."""
    settings = settings or get_settings()
    names: list[StoreName] = ["weaviate", "chroma"]
    if settings.pinecone_api_key is not None:
        names.append("pinecone")
    return names


def get_store(
    name: StoreName,
    chunker: ChunkerName = "markdown_section",
    *,
    dimension: int = 384,
    settings: Settings | None = None,
) -> VectorStore:
    """Construct the named store for one chunker's collection."""
    settings = settings or get_settings()
    if name == "chroma":
        from opspilot.rag.stores.chroma_store import ChromaStore

        return ChromaStore(settings.chroma_dir, chunker)
    if name == "weaviate":
        from opspilot.rag.stores.weaviate_store import WeaviateStore

        return WeaviateStore(settings.weaviate_url, settings.weaviate_grpc_port, chunker)
    if name == "pinecone":
        if settings.pinecone_api_key is None:
            raise StoreUnavailableError("set PINECONE_API_KEY to use the optional Pinecone store")
        from opspilot.rag.stores.pinecone_store import PineconeStore

        return PineconeStore(
            settings.pinecone_api_key.get_secret_value(),
            dimension,
            settings.data_dir / "pinecone",
            chunker,
            cloud=settings.pinecone_cloud,
            region=settings.pinecone_region,
        )
    raise ValueError(f"unknown store {name!r}")


__all__ = ["STORE_NAMES", "StoreName", "StoreUnavailableError", "VectorStore", "get_store"]
