from pathlib import Path

import pytest

from opspilot.rag.bm25 import BM25Index, tokenize
from opspilot.rag.fusion import rrf
from opspilot.rag.models import Chunk, Filters, SearchHit
from opspilot.rag.stores.base import rrf_hybrid


def chunk(cid: str, text: str = "", doc_type: str = "runbook", **kw: object) -> Chunk:
    return Chunk(
        chunk_id=cid,
        doc_id=f"rb-{cid}",
        doc_type=doc_type,  # type: ignore[arg-type]
        title=cid,
        section="",
        index=0,
        text=text,
        **kw,  # type: ignore[arg-type]
    )


def test_rrf_matches_hand_computed_example() -> None:
    # k = 60. a: 1/61. b: 1/62 + 1/61. c: 1/63 + 1/62. d: 1/63.
    fused = dict(rrf([["a", "b", "c"], ["b", "c", "d"]], k=60))
    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert fused["c"] == pytest.approx(1 / 63 + 1 / 62)
    assert fused["d"] == pytest.approx(1 / 63)
    assert [item for item, _ in rrf([["a", "b", "c"], ["b", "c", "d"]])] == ["b", "c", "a", "d"]


def test_rrf_weights_and_ties() -> None:
    fused = rrf([["a"], ["b"]], weights=[0.25, 0.75])
    assert fused[0] == ("b", pytest.approx(0.75 / 61))
    assert [item for item, _ in rrf([["x"], ["y"]])] == ["x", "y"]  # tie: first seen wins
    with pytest.raises(ValueError, match="one weight per ranking"):
        rrf([["a"]], weights=[1.0, 2.0])


def test_rrf_hybrid_alpha_extremes() -> None:
    dense = [SearchHit(chunk=chunk("a"), score=0.9), SearchHit(chunk=chunk("b"), score=0.8)]
    keyword = [SearchHit(chunk=chunk("b"), score=7.0), SearchHit(chunk=chunk("c"), score=3.0)]
    assert [h.chunk.chunk_id for h in rrf_hybrid(dense, keyword, 3, alpha=1.0)][:2] == ["a", "b"]
    assert [h.chunk.chunk_id for h in rrf_hybrid(dense, keyword, 3, alpha=0.0)][:2] == ["b", "c"]
    assert rrf_hybrid(dense, keyword, 3, alpha=0.5)[0].chunk.chunk_id == "b"


def test_tokenize_lowercases_and_drops_stopwords() -> None:
    assert tokenize("The Pod is OOMKilled, exit-code 137!") == [
        "pod",
        "oomkilled",
        "exit",
        "code",
        "137",
    ]


def test_bm25_search_filters_and_persistence(tmp_path: Path) -> None:
    index = BM25Index(
        [
            chunk("oom", "container oomkilled exit code 137 memory limit"),
            chunk("dns", "dns lookup failed coredns", doc_type="postmortem"),
            chunk("img", "imagepullbackoff manifest unknown"),
        ]
    )
    hits = index.search("oomkilled memory", k=5)
    assert [h.chunk.chunk_id for h in hits] == ["oom"]
    assert index.search("dns", k=5, filters=Filters(doc_types=["runbook"])) == []
    path = tmp_path / "bm25.json"
    index.save(path)
    reloaded = BM25Index.load(path)
    assert len(reloaded) == 3
    assert [h.chunk.chunk_id for h in reloaded.search("manifest", k=1)] == ["img"]
    assert len(BM25Index.load(tmp_path / "missing.json")) == 0
    assert BM25Index().search("anything", k=3) == []


def test_filters_match() -> None:
    c = chunk("a", categories=["OOM_KILLED"], services=["payments-api"])
    assert Filters().matches(c)
    assert Filters(categories=["OOM_KILLED"], services=["payments-api"]).matches(c)
    assert not Filters(categories=["PVC_PENDING"]).matches(c)
    assert not Filters(services=["redis"]).matches(c)
    assert not Filters(doc_types=["postmortem"]).matches(c)
