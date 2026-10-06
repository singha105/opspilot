import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from opspilot.evals import metrics
from opspilot.evals.report import render_chart, render_markdown
from opspilot.evals.retrieval import (
    ConfigResult,
    EvalQuery,
    QueryResult,
    RetrievalConfig,
    aggregate,
    choose_default,
    leakage,
    load_queries,
    matrix,
    title_overlap,
    unknown_documents,
)
from opspilot.rag.loader import load_documents

ROOT = Path(__file__).parents[2]
RANKED = ["a", "b", "c", "d", "e"]
RELEVANT = {"b": 2, "d": 1}


def test_recall_hand_computed() -> None:
    assert metrics.recall_at_k(RANKED, RELEVANT, 1) == 0.0
    assert metrics.recall_at_k(RANKED, RELEVANT, 3) == 1.0
    assert metrics.recall_at_k(RANKED, {"x": 1}, 5) == 0.0  # no primary


def test_mrr_hand_computed() -> None:
    assert metrics.mrr_at_k(RANKED, RELEVANT, 10) == 0.5
    assert metrics.mrr_at_k(RANKED, {"e": 2}, 4) == 0.0


def test_ndcg_hand_computed() -> None:
    # DCG  = 3/log2(3) + 1/log2(5)       = 1.892789 + 0.430677 = 2.323466
    # IDCG = 3/log2(2) + 1/log2(3)       = 3.0      + 0.630930 = 3.630930
    assert metrics.ndcg_at_k(RANKED, RELEVANT, 5) == pytest.approx(2.323466 / 3.630930, abs=1e-6)
    assert metrics.ndcg_at_k(["b", "d"], RELEVANT, 5) == pytest.approx(1.0)
    assert metrics.ndcg_at_k(RANKED, {}, 5) == 0.0


def test_unique_docs_and_percentile() -> None:
    assert metrics.unique_docs(["a", "a", "b", "a", "c"]) == ["a", "b", "c"]
    assert metrics.percentile([5, 1, 3, 2, 4], 50) == 3
    assert metrics.percentile(list(range(1, 101)), 95) == 95
    assert metrics.percentile([], 95) == 0.0


def test_query_set_is_valid_and_leak_free() -> None:
    queries = load_queries(ROOT / "evals" / "retrieval" / "queries.jsonl")
    docs = load_documents(ROOT / "knowledge")
    assert len(queries) == 60
    assert {q.type for q in queries} == {"symptom", "log", "hard"}
    assert unknown_documents(queries, docs) == []
    assert leakage(queries, docs) == []


def test_query_needs_exactly_one_primary() -> None:
    with pytest.raises(ValidationError, match="exactly one primary"):
        EvalQuery(qid="q001", type="symptom", query="something long", relevant={"a": 1})


def test_leakage_flags_title_copies() -> None:
    assert (
        title_overlap(
            "container oomkilled exit code 137 again", "Container OOMKilled / exit code 137"
        )
        == 1.0
    )
    docs = load_documents(ROOT / "knowledge")
    copy = EvalQuery(
        qid="q999",
        type="symptom",
        query="Container OOMKilled exit code 137",
        relevant={"rb-oom-killed": 2},
    )
    assert leakage([copy], docs) == [("q999", "rb-oom-killed", 1.0)]


def test_load_queries_rejects_duplicates(tmp_path: Path) -> None:
    line = json.dumps(
        {"qid": "q001", "type": "log", "query": "a long enough query", "relevant": {"x": 2}}
    )
    path = tmp_path / "q.jsonl"
    path.write_text(line + "\n" + line + "\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_queries(path)


def test_matrix_shape() -> None:
    configs = matrix(["weaviate", "chroma"])
    assert len(configs) == 2 * 2 * 3 * 2 + 4
    assert len({c.label for c in configs}) == len(configs)
    assert len(matrix(["chroma"])) == 12


def _result(label_mode: str, ndcg: float, p95: float, rerank: bool = False) -> ConfigResult:
    config = RetrievalConfig(
        store="chroma", chunker="markdown_section", mode=label_mode, rerank=rerank
    )  # type: ignore[arg-type]
    rows = [
        QueryResult(
            qid="q001",
            type="symptom",
            ranked_docs=["a"],
            recall_at_1=1,
            recall_at_3=1,
            recall_at_5=1,
            mrr_at_10=1,
            ndcg_at_5=ndcg,
            latency_ms=p95,
        )
    ]
    return aggregate(config, rows)


def test_choose_default_respects_latency_budget() -> None:
    fast = _result("dense", 0.70, 50)
    best_but_slow = _result("hybrid", 0.90, 450, rerank=True)
    good = _result("hybrid", 0.80, 120)
    assert choose_default([fast, best_but_slow, good]) == good
    assert choose_default([best_but_slow]) is None


def test_reports_render(tmp_path: Path) -> None:
    results = [_result("dense", 0.70, 50), _result("hybrid", 0.80, 120)]
    md = render_markdown(
        results,
        results[1],
        {
            "date": "2026-10-05",
            "machine": "test",
            "queries": 1,
            "documents": 52,
            "embedding_model": "m",
            "reranker_model": "r",
        },
    )
    assert "| chroma | markdown_section | hybrid α=0.5 **(default)** | off |" in md  # noqa: RUF001
    assert md.index("hybrid") < md.index("| dense")  # sorted by nDCG@5
    chart = tmp_path / "chart.png"
    render_chart(results, results[1], chart)
    assert chart.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
