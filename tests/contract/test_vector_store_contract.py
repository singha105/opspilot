"""One contract every VectorStore must satisfy.

Chroma runs everywhere (local files only). Weaviate needs `make infra-up` and Pinecone
needs PINECONE_API_KEY; both are marked integration, skip when unavailable, and use
throwaway collections so real indexes are never touched.
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from opspilot.rag.models import Chunk, Filters
from opspilot.rag.stores.base import VectorStore

CHUNKS = [
    Chunk(
        chunk_id="c-oom",
        doc_id="rb-oom-killed",
        doc_type="runbook",
        title="OOM",
        section="Symptoms",
        index=0,
        text="container oomkilled exit code 137 memory limit exceeded",
        categories=["OOM_KILLED"],
        services=["payments-api"],
    ),
    Chunk(
        chunk_id="c-dns",
        doc_id="rb-dns",
        doc_type="runbook",
        title="DNS",
        section="Symptoms",
        index=0,
        text="dns lookups fail temporary failure in name resolution coredns",
        services=["orders-api"],
    ),
    Chunk(
        chunk_id="c-pm",
        doc_id="pm-2026-03-leak",
        doc_type="postmortem",
        title="Leak",
        section="Summary",
        index=0,
        text="payments memory leak restarts every forty minutes oomkilled",
        categories=["OOM_KILLED"],
        services=["payments-api"],
    ),
]
VECTORS = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.9, 0.0, 0.1, 0.0]]
OOM_QUERY = [1.0, 0.0, 0.05, 0.0]


def _weaviate_up() -> bool:
    import httpx

    from opspilot.config import get_settings

    try:
        url = get_settings().weaviate_url + "/v1/.well-known/ready"
        return httpx.get(url, timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(
    params=[
        "chroma",
        pytest.param("weaviate", marks=pytest.mark.integration),
        pytest.param("pinecone", marks=pytest.mark.integration),
    ]
)
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[VectorStore]:
    name = request.param
    suffix = uuid.uuid4().hex[:8]
    if name == "chroma":
        from opspilot.rag.stores.chroma_store import ChromaStore

        backend: VectorStore = ChromaStore(tmp_path / "chroma", collection=f"contract-{suffix}")
    elif name == "weaviate":
        if not _weaviate_up():
            pytest.skip("Weaviate is not running (`make infra-up`)")
        from opspilot.config import get_settings
        from opspilot.rag.stores.weaviate_store import WeaviateStore

        s = get_settings()
        backend = WeaviateStore(
            s.weaviate_url, s.weaviate_grpc_port, collection=f"ContractTest{suffix}"
        )
    else:
        if not os.environ.get("PINECONE_API_KEY"):
            pytest.skip("PINECONE_API_KEY not set (Pinecone is optional)")
        from opspilot.rag.stores.pinecone_store import PineconeStore

        backend = PineconeStore(
            os.environ["PINECONE_API_KEY"], 4, tmp_path, index_name=f"contract-{suffix}"
        )
    backend.reset()
    backend.upsert(CHUNKS, VECTORS)
    yield backend
    backend.reset()
    if name == "weaviate":
        backend._client.collections.delete(f"ContractTest{suffix}")  # type: ignore[attr-defined]
        backend.close()  # type: ignore[attr-defined]
    if name == "pinecone":
        backend._pc.delete_index(f"contract-{suffix}")  # type: ignore[attr-defined]


def test_count_and_idempotent_upsert(store: VectorStore) -> None:
    assert store.count() == 3
    store.upsert(CHUNKS, VECTORS)
    assert store.count() == 3


def test_dense_search_orders_by_similarity(store: VectorStore) -> None:
    hits = store.dense_search(OOM_QUERY, k=2)
    assert [h.chunk.chunk_id for h in hits] == ["c-oom", "c-pm"]
    assert hits[0].score >= hits[1].score
    assert hits[0].chunk.categories == ["OOM_KILLED"]


def test_keyword_search_finds_terms(store: VectorStore) -> None:
    hits = store.keyword_search("coredns name resolution", k=3)
    assert hits[0].chunk.chunk_id == "c-dns"


def test_hybrid_search_combines_both(store: VectorStore) -> None:
    hits = store.hybrid_search("memory leak forty minutes", OOM_QUERY, k=3, alpha=0.5)
    assert {h.chunk.chunk_id for h in hits[:2]} == {"c-oom", "c-pm"}


def test_filters(store: VectorStore) -> None:
    only_pm = Filters(doc_types=["postmortem"])
    assert [h.chunk.chunk_id for h in store.dense_search(OOM_QUERY, 3, only_pm)] == ["c-pm"]
    by_service = Filters(services=["orders-api"])
    assert [h.chunk.chunk_id for h in store.keyword_search("dns oomkilled", 3, by_service)] == [
        "c-dns"
    ]
    by_category = Filters(categories=["OOM_KILLED"], doc_types=["runbook"])
    hits = store.hybrid_search("oomkilled", OOM_QUERY, 3, filters=by_category)
    assert [h.chunk.chunk_id for h in hits] == ["c-oom"]


def test_reset_empties(store: VectorStore) -> None:
    store.reset()
    assert store.count() == 0
    assert store.dense_search(OOM_QUERY, 3) == []
    assert store.keyword_search("oomkilled", 3) == []
