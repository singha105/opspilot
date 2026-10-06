"""Retrieval evaluation: query set, leakage check, config matrix and reports."""

import json
import statistics
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from opspilot.evals.metrics import (
    mrr_at_k,
    ndcg_at_k,
    percentile,
    recall_at_k,
    unique_docs,
)
from opspilot.rag.bm25 import tokenize
from opspilot.rag.models import ChunkerName, Document, SearchMode
from opspilot.rag.retriever import Retriever

LEAKAGE_THRESHOLD = 0.6
RESULT_CHUNKS = 20  # every candidate after dedupe, so MRR@10 sees up to 10 documents
LATENCY_BUDGET_MS = 300.0


class EvalQuery(BaseModel):
    """One graded query: ``relevant`` maps doc id to 2 (primary) or 1 (secondary)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    qid: str = Field(pattern=r"^q\d{3}$")
    type: Literal["symptom", "log", "hard"]
    query: str = Field(min_length=10)
    relevant: dict[str, Literal[1, 2]]

    @field_validator("relevant")
    @classmethod
    def _one_primary(cls, value: dict[str, int]) -> dict[str, int]:
        if sum(1 for g in value.values() if g == 2) != 1:
            raise ValueError("each query needs exactly one primary (grade 2) document")
        return value

    @property
    def primary(self) -> str:
        return next(d for d, g in self.relevant.items() if g == 2)


class RetrievalConfig(BaseModel):
    """One cell of the evaluation matrix."""

    model_config = ConfigDict(frozen=True)

    store: Literal["weaviate", "chroma", "pinecone"]
    chunker: ChunkerName
    mode: SearchMode
    rerank: bool
    alpha: float = 0.5

    @property
    def label(self) -> str:
        mode = f"hybrid a={self.alpha:g}" if self.mode == "hybrid" else self.mode
        rerank = "+rerank" if self.rerank else ""
        return f"{self.store}/{self.chunker}/{mode}{rerank}"


class QueryResult(BaseModel):
    qid: str
    type: str
    ranked_docs: list[str]
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr_at_10: float
    ndcg_at_5: float
    latency_ms: float


class ConfigResult(BaseModel):
    config: RetrievalConfig
    label: str
    queries: int
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr_at_10: float
    ndcg_at_5: float
    p50_ms: float
    p95_ms: float
    by_type: dict[str, dict[str, float]]
    per_query: list[QueryResult]


def load_queries(path: Path) -> list[EvalQuery]:
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    queries = [EvalQuery.model_validate_json(line) for line in lines]
    if len({q.qid for q in queries}) != len(queries):
        raise ValueError("duplicate qids")
    return queries


def title_overlap(query: str, title: str) -> float:
    """Share of the title's tokens (stopwords removed) that also appear in the query."""
    title_tokens = set(tokenize(title))
    if not title_tokens:
        return 0.0
    return len(title_tokens & set(tokenize(query))) / len(title_tokens)


def leakage(
    queries: Sequence[EvalQuery], docs: Sequence[Document], threshold: float = LEAKAGE_THRESHOLD
) -> list[tuple[str, str, float]]:
    """Queries whose wording copies more than ``threshold`` of their primary doc's title."""
    titles = {d.doc_id: d.title for d in docs}
    flagged = []
    for q in queries:
        if q.primary not in titles:
            raise KeyError(f"{q.qid}: unknown primary document {q.primary}")
        overlap = title_overlap(q.query, titles[q.primary])
        if overlap > threshold:
            flagged.append((q.qid, q.primary, round(overlap, 2)))
    return flagged


def unknown_documents(queries: Sequence[EvalQuery], docs: Sequence[Document]) -> list[str]:
    known = {d.doc_id for d in docs}
    return sorted({f"{q.qid}:{d}" for q in queries for d in q.relevant if d not in known})


def evaluate(
    retriever: Retriever, config: RetrievalConfig, queries: Sequence[EvalQuery]
) -> ConfigResult:
    """Run every query through one configured retriever and aggregate the metrics."""
    retriever.retrieve("warm-up query", k=1, mode=config.mode, rerank=config.rerank)
    rows: list[QueryResult] = []
    for q in queries:
        start = time.perf_counter()
        result = retriever.retrieve(
            q.query, k=RESULT_CHUNKS, mode=config.mode, rerank=config.rerank
        )
        latency = (time.perf_counter() - start) * 1000
        ranked = unique_docs([c.doc_id for c in result.chunks])
        rows.append(
            QueryResult(
                qid=q.qid,
                type=q.type,
                ranked_docs=ranked[:5],
                recall_at_1=round(recall_at_k(ranked, q.relevant, 1), 4),
                recall_at_3=round(recall_at_k(ranked, q.relevant, 3), 4),
                recall_at_5=round(recall_at_k(ranked, q.relevant, 5), 4),
                mrr_at_10=round(mrr_at_k(ranked, q.relevant, 10), 4),
                ndcg_at_5=round(ndcg_at_k(ranked, q.relevant, 5), 4),
                latency_ms=round(latency, 2),
            )
        )
    return aggregate(config, rows)


def _mean(rows: Sequence[QueryResult], field: str) -> float:
    return round(statistics.fmean(getattr(r, field) for r in rows), 4) if rows else 0.0


def aggregate(config: RetrievalConfig, rows: list[QueryResult]) -> ConfigResult:
    fields = ("recall_at_1", "recall_at_3", "recall_at_5", "mrr_at_10", "ndcg_at_5")
    by_type = {
        t: {f: _mean([r for r in rows if r.type == t], f) for f in fields}
        for t in sorted({r.type for r in rows})
    }
    latencies = [r.latency_ms for r in rows]
    return ConfigResult(
        config=config,
        label=config.label,
        queries=len(rows),
        **{f: _mean(rows, f) for f in fields},
        p50_ms=round(percentile(latencies, 50), 1),
        p95_ms=round(percentile(latencies, 95), 1),
        by_type=by_type,
        per_query=rows,
    )


def matrix(stores: Sequence[str]) -> list[RetrievalConfig]:
    """store x mode x rerank x chunker, plus a Weaviate alpha sweep on the default chunker."""
    configs = [
        RetrievalConfig(store=store, chunker=chunker, mode=mode, rerank=rerank)
        for chunker in ("markdown_section", "fixed")
        for store in stores
        for mode in ("dense", "keyword", "hybrid")
        for rerank in (False, True)
    ]
    if "weaviate" in stores:
        configs += [
            RetrievalConfig(
                store="weaviate", chunker="markdown_section", mode="hybrid", rerank=rerank, alpha=a
            )
            for a in (0.25, 0.75)
            for rerank in (False, True)
        ]
    return configs


def choose_default(
    results: Sequence[ConfigResult], budget_ms: float = LATENCY_BUDGET_MS
) -> ConfigResult | None:
    """Best nDCG@5 among configs with p95 latency under budget (ties: lower p95)."""
    eligible = [r for r in results if r.p95_ms < budget_ms]
    if not eligible:
        return None
    return max(eligible, key=lambda r: (r.ndcg_at_5, -r.p95_ms))


RetrieverFactory = Callable[[RetrievalConfig], Retriever]


def run_matrix(
    configs: Sequence[RetrievalConfig],
    queries: Sequence[EvalQuery],
    factory: RetrieverFactory,
    on_result: Callable[[ConfigResult], None] | None = None,
) -> list[ConfigResult]:
    results = []
    for config in configs:
        retriever = factory(config)
        try:
            result = evaluate(retriever, config, queries)
        finally:
            close = getattr(retriever.store, "close", None)
            if callable(close):
                close()
        if on_result:
            on_result(result)
        results.append(result)
    return results


def save_json(
    results: Sequence[ConfigResult],
    default: ConfigResult | None,
    path: Path,
    meta: dict[str, object],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **meta,
        "default": default.label if default else None,
        "results": [r.model_dump() for r in results],
    }
    # Compact JSON keeps the committed results file small (one row per query per config).
    path.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
