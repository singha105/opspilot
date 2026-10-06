"""``opspilot eval`` commands."""

import datetime as dt
import json
import platform
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console

from opspilot.config import get_settings
from opspilot.evals.retrieval import (
    ConfigResult,
    RetrievalConfig,
    choose_default,
    leakage,
    load_queries,
    matrix,
    run_matrix,
    save_json,
    unknown_documents,
)
from opspilot.rag.loader import load_documents
from opspilot.rag.stores import available_stores

app = typer.Typer(help="Evaluate OpsPilot components.")
console = Console(width=140)

EVAL_DIR = Path("evals")
QUERIES = EVAL_DIR / "retrieval" / "queries.jsonl"
SMOKE = EVAL_DIR / "retrieval" / "smoke.json"


def _factory(config: RetrievalConfig):  # type: ignore[no-untyped-def]
    from opspilot.rag.pipeline import build_retriever

    return build_retriever(config.store, config.chunker, rerank=config.rerank, alpha=config.alpha)


def _print(result: ConfigResult) -> None:
    console.print(
        f"{result.label:<52} R@5={result.recall_at_5:.3f} MRR@10={result.mrr_at_10:.3f} "
        f"nDCG@5={result.ndcg_at_5:.3f} p95={result.p95_ms:.0f}ms"
    )


@app.command("check-queries")
def check_queries(queries: Path = QUERIES) -> None:
    """Fail if a query names unknown documents or copies its primary document's title."""
    docs = load_documents(get_settings().knowledge_dir)
    qs = load_queries(queries)
    problems = unknown_documents(qs, docs)
    for qid, doc_id, overlap in leakage(qs, docs):
        problems.append(f"{qid}: {overlap:.0%} of the title of {doc_id} appears in the query")
    for p in problems:
        console.print(f"[red]{p}[/red]")
    if problems:
        raise typer.Exit(code=1)
    console.print(f"{len(qs)} queries OK: no unknown documents, no title leakage")


@app.command()
def retrieval(
    configs: Annotated[
        Literal["all", "smoke"], typer.Option(help="Full matrix, or the CI smoke check.")
    ] = "all",
    queries: Path = QUERIES,
) -> None:
    """Run the retrieval evaluation matrix (or the smoke check) and write the reports."""
    if configs == "smoke":
        _smoke(queries)
        return
    from opspilot.evals.report import render_chart, render_markdown

    settings = get_settings()
    qs = load_queries(queries)
    stores = [s for s in available_stores(settings)]
    results = run_matrix(matrix(stores), qs, _factory, on_result=_print)
    default = choose_default(results)
    today = dt.date.today().isoformat()
    meta: dict[str, object] = {
        "date": today,
        "machine": f"{platform.machine()} {platform.system()}, {platform.python_version()}",
        "queries": len(qs),
        "documents": len(load_documents(settings.knowledge_dir)),
        "embedding_model": settings.embedding_model,
        "reranker_model": settings.reranker_model,
    }
    save_json(results, default, EVAL_DIR / "results" / f"retrieval-{today}.json", meta)
    (EVAL_DIR / "retrieval" / "RESULTS.md").write_text(render_markdown(results, default, meta))
    render_chart(results, default, EVAL_DIR / "retrieval" / "retrieval_ndcg.png")
    console.print(f"default: {default.label if default else 'none under budget'}")


def _smoke(queries: Path) -> None:
    spec = json.loads(SMOKE.read_text())
    wanted = set(spec["qids"])
    qs = [q for q in load_queries(queries) if q.qid in wanted]
    config = RetrievalConfig.model_validate(spec["config"])
    (result,) = run_matrix([config], qs, _factory, on_result=_print)
    floor = spec["baseline_recall_at_5"] - spec["tolerance"]
    if result.recall_at_5 < floor:
        console.print(f"[red]Recall@5 {result.recall_at_5:.3f} is below {floor:.3f}[/red]")
        raise typer.Exit(code=1)
    console.print(f"smoke OK: Recall@5 {result.recall_at_5:.3f} >= {floor:.3f}")
