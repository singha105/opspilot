"""Data models shared by the RAG pipeline."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from opspilot.models import RootCauseCategory
from opspilot.rag.schema import DocType

SearchMode = Literal["dense", "keyword", "hybrid"]
ChunkerName = Literal["markdown_section", "fixed"]


class Document(BaseModel):
    """One knowledge-base document: validated metadata plus its Markdown body."""

    model_config = ConfigDict(frozen=True)

    doc_id: str
    doc_type: DocType
    title: str
    categories: list[RootCauseCategory] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source_path: str
    license: str
    body: str


class Chunk(BaseModel):
    """A retrievable piece of a document. ``text`` is what gets embedded and indexed."""

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    doc_id: str
    doc_type: DocType
    title: str
    section: str
    index: int
    text: str
    categories: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)


class Filters(BaseModel):
    """Optional metadata filters; a field left as None does not filter."""

    model_config = ConfigDict(frozen=True)

    doc_types: list[DocType] | None = None
    categories: list[str] | None = None
    services: list[str] | None = None

    def matches(self, chunk: Chunk) -> bool:
        """Apply the filters in Python (used by local keyword indexes)."""
        if self.doc_types and chunk.doc_type not in self.doc_types:
            return False
        if self.categories and not set(self.categories) & set(chunk.categories):
            return False
        return not (self.services and not set(self.services) & set(chunk.services))


class SearchHit(BaseModel):
    """A chunk returned by a store, with the store's own score (higher is better)."""

    model_config = ConfigDict(frozen=True)

    chunk: Chunk
    score: float


class RetrievedChunk(BaseModel):
    """A chunk in the final, cited retrieval result."""

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    doc_id: str
    doc_type: DocType
    title: str
    section: str
    text: str
    score: float
    rank: int
    citation_id: str


class RetrievalResult(BaseModel):
    """Retrieved chunks plus per-stage timings in milliseconds."""

    chunks: list[RetrievedChunk]
    timings_ms: dict[str, float]
