"""Real model checks (download fastembed and FlashRank models on first run)."""

import pytest

from opspilot.rag.models import Chunk, SearchHit
from opspilot.rag.pipeline import default_embedder, default_reranker

pytestmark = pytest.mark.integration


def _hit(cid: str, text: str) -> SearchHit:
    chunk = Chunk(
        chunk_id=cid, doc_id=cid, doc_type="runbook", title=cid, section="", index=0, text=text
    )
    return SearchHit(chunk=chunk, score=0.0)


def test_bge_small_dimension_and_query_passage_similarity() -> None:
    embedder = default_embedder()
    assert embedder.dimension == 384
    oom, dns = embedder.embed_passages(
        ["container killed with OOMKilled, exit code 137", "DNS lookup failed for redis"]
    )
    query = embedder.embed_query("pod out of memory exit 137")

    def cos(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert cos(query, oom) > cos(query, dns)


def test_flashrank_reorders_by_relevance() -> None:
    hits = [_hit("dns", "coredns name resolution failures"), _hit("oom", "OOMKilled exit code 137")]
    ranked = default_reranker().rerank("container exit code 137 out of memory", hits)
    assert ranked[0].chunk.chunk_id == "oom"
