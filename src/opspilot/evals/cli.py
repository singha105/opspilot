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


SPLITS = EVAL_DIR / "agent" / "splits.yaml"
Split = Literal["dev", "test", "control", "all"]


def _mark(value: bool | None) -> str:
    return "-" if value is None else ("ok" if value else "MISS")


@app.command()
def agent(
    configs: Annotated[str, typer.Option(help="Comma list of C0..C4.")] = "C3",
    split: Annotated[Split, typer.Option(help="dev, test, control or all.")] = "all",
    limit: Annotated[int | None, typer.Option(help="Run only the first N cases.")] = None,
    resume: Annotated[bool, typer.Option(help="Reuse cached outcomes with the same key.")] = False,
    cases: Annotated[str | None, typer.Option(help="Comma list of case ids.")] = None,
) -> None:
    """Replay the agent on the scenario catalog and score it (concurrency 1)."""
    from opspilot.evals.agent_eval import CONFIGS, AgentEval
    from opspilot.evals.cases import load_cases
    from opspilot.evals.scoring import EvalRow, proportion

    settings = get_settings()
    unknown = [c for c in configs.split(",") if c not in CONFIGS]
    if unknown:
        raise typer.BadParameter(f"unknown configs {unknown}; choose from {list(CONFIGS)}")
    all_cases = load_cases(settings.scenarios_dir, SPLITS)
    chosen = [c for c in all_cases.values() if split == "all" or c.split == split]
    if cases:
        wanted = cases.split(",")
        chosen = [c for c in chosen if c.id in wanted]
    chosen = chosen[:limit] if limit else chosen
    runner = AgentEval(settings, Path.cwd(), EVAL_DIR / "results", resume=resume)
    for name in configs.split(","):
        config = CONFIGS[name]
        console.print(f"[bold]{name}[/bold] {config.label}: {len(chosen)} cases")
        done = 0

        def show(row: EvalRow, cached: bool) -> None:
            nonlocal done
            done += 1
            note = " (cached)" if cached else ""
            console.print(
                f"  [{done}/{len(chosen)}] {row.scenario_id}: {row.category_pred} "
                f"category {_mark(row.category_correct)}, component "
                f"{_mark(row.component_correct)}, remediation "
                f"{_mark(row.remediation_acceptable)}, {row.latency_s:.0f}s{note}"
            )

        rows = runner.evaluate(chosen, config, on_row=show)
        accuracy = proportion(rows, "category_correct")
        console.print(
            f"{name}: category {accuracy.text()} (95% CI {accuracy.ci_text()}) "
            f"-> {runner.output_path(config)}"
        )


@app.command()
def live(
    cases: Annotated[
        str | None, typer.Option(help="Comma list of case ids (default: the live subset).")
    ] = None,
) -> None:
    """Run the live subset end to end on the demo cluster (inject, agent, approve, verify)."""
    import asyncio

    import yaml

    from opspilot.evals.agent_eval import DEFAULT_CONFIG
    from opspilot.evals.cases import load_cases
    from opspilot.evals.live_eval import run_live_case
    from opspilot.faults.injector import Injector
    from opspilot.faults.scenario import load_scenarios
    from opspilot.kube import admin_client

    settings = get_settings()
    all_cases = load_cases(settings.scenarios_dir, SPLITS)
    ids = cases.split(",") if cases else yaml.safe_load(SPLITS.read_text())["live"]
    scenarios = load_scenarios(settings.scenarios_dir)
    injector = Injector(
        admin_client(settings.admin_context),
        context=settings.admin_context,
        base_dir=settings.demo_base_dir,
    )
    out = EVAL_DIR / "results" / f"live-{dt.date.today().isoformat()}.jsonl"
    rows = []
    for case_id in ids:
        console.print(f"[bold]{case_id}[/bold]: injecting")
        row = asyncio.run(
            run_live_case(
                all_cases[case_id],
                scenarios[case_id],
                injector,
                settings,
                DEFAULT_CONFIG,
                settings.runs_dir / "eval-live",
            )
        )
        rows.append(row)
        out.write_text("".join(r.model_dump_json() + "\n" for r in rows))
        console.print(
            f"  {row.category_pred} (category {_mark(row.category_correct)}), "
            f"{row.decision or 'no decision'}: {row.decision_reason}; verification "
            f"{row.verification}; {row.agent_s:.0f}s"
        )
    recovered = sum(r.recovered for r in rows)
    console.print(f"recovered {recovered}/{len(rows)} -> {out}")


REPORT = EVAL_DIR / "REPORT.md"
README = Path("README.md")
FAILURE_NOTES = EVAL_DIR / "agent" / "failure_analysis.yaml"
CHARTS = EVAL_DIR / "charts"


@app.command()
def report(
    headline_config: Annotated[str, typer.Option(help="Config for the headline.")] = "C3",
) -> None:
    """Rewrite the generated blocks of evals/REPORT.md and README.md from the result files."""
    from opspilot.evals import agent_report as ar

    results = EVAL_DIR / "results"
    by_config = ar.latest_rows(results)
    if headline_config not in by_config:
        raise typer.BadParameter(f"no results for {headline_config} in {results}")
    main = by_config[headline_config]
    notes = ar.load_failure_notes(FAILURE_NOTES)
    headline = ar.headline(main, headline_config)
    blocks = {
        "headline": headline,
        "splits": ar.splits_table(main, headline_config),
        "ablation": ar.ablation(by_config),
        "per-category": ar.per_category(main),
        "safety": ar.safety(by_config),
        "live": ar.live_table(ar.latest_live(results)),
        "efficiency": ar.efficiency(by_config),
        "failures": ar.failures(by_config, notes),
    }
    text = REPORT.read_text()
    for name, body in blocks.items():
        text = ar.replace_block(text, name, body)
    REPORT.write_text(text)
    README.write_text(ar.replace_block(README.read_text(), "results", headline))
    CHARTS.mkdir(parents=True, exist_ok=True)
    ar.chart_ablation(by_config, CHARTS / "accuracy-by-config.png")
    ar.chart_categories(main, CHARTS / "accuracy-by-category.png")
    unclassified = ar.failures(by_config, notes).count("| unclassified |")
    console.print(f"updated {REPORT} and {README}; {unclassified} misses still unclassified")
