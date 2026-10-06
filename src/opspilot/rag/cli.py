"""``opspilot kb`` commands: ingest, search and stats."""

from collections import Counter
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.table import Table

from opspilot.config import get_settings
from opspilot.rag.chunking import chunk_documents
from opspilot.rag.loader import load_documents
from opspilot.rag.models import ChunkerName, SearchMode
from opspilot.rag.stores import STORE_NAMES, StoreName, available_stores, get_store

app = typer.Typer(help="Build and query the knowledge base.")
console = Console()

StoreChoice = Literal["weaviate", "chroma", "pinecone", "all"]
ChunkerChoice = Literal["markdown_section", "fixed", "all"]
CHUNKERS: tuple[ChunkerName, ...] = ("markdown_section", "fixed")


def _stores(choice: StoreChoice) -> list[StoreName]:
    return available_stores() if choice == "all" else [choice]


def _chunkers(choice: ChunkerChoice) -> list[ChunkerName]:
    return list(CHUNKERS) if choice == "all" else [choice]


@app.command()
def ingest(
    store: Annotated[StoreChoice, typer.Option(help="Target store.")] = "all",
    chunker: Annotated[ChunkerChoice, typer.Option(help="Chunking strategy.")] = "all",
) -> None:
    """(Re)index the knowledge base. Idempotent: each collection is rebuilt from scratch."""
    from opspilot.rag.pipeline import ingest as run_ingest

    reports = run_ingest(_stores(store), _chunkers(chunker))
    table = Table("store", "chunker", "documents", "chunks", "stored")
    for r in reports:
        table.add_row(r.store, r.chunker, str(r.documents), str(r.chunks), str(r.stored))
    console.print(table)
    if any(r.stored != r.chunks for r in reports):
        console.print("[red]stored count differs from chunk count[/red]")
        raise typer.Exit(code=1)


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search text.")],
    store: Annotated[StoreName, typer.Option(help="Store to query.")] = "weaviate",
    mode: Annotated[SearchMode, typer.Option(help="dense, keyword or hybrid.")] = "hybrid",
    rerank: Annotated[bool, typer.Option(help="Rerank with FlashRank.")] = True,
    chunker: Annotated[ChunkerName, typer.Option(help="Collection to query.")] = (
        "markdown_section"
    ),
    k: Annotated[int, typer.Option(help="Number of results.")] = 6,
    alpha: Annotated[float, typer.Option(help="Hybrid weight of the dense side.")] = 0.5,
) -> None:
    """Search the knowledge base and print cited results."""
    from opspilot.rag.pipeline import build_retriever

    retriever = build_retriever(store, chunker, rerank=rerank, alpha=alpha)
    result = retriever.retrieve(query, k=k, mode=mode, rerank=rerank)
    for chunk in result.chunks:
        console.print(
            f"[bold]{chunk.citation_id}[/bold] {chunk.doc_id} "
            f"[dim]{chunk.section or '-'}[/dim] score={chunk.score:.4f}"
        )
        console.print("    " + chunk.text[:200].replace("\n", " ") + "...")
    timings = ", ".join(f"{k}={v:.0f}ms" for k, v in result.timings_ms.items())
    console.print(f"[dim]{timings}[/dim]")


@app.command()
def stats() -> None:
    """Document and chunk counts, and what each available store holds."""
    settings = get_settings()
    docs = load_documents(settings.knowledge_dir)
    by_type = Counter(d.doc_type for d in docs)
    console.print("documents: " + ", ".join(f"{t}={n}" for t, n in sorted(by_type.items())))
    table = Table("store", "chunker", "expected chunks", "stored")
    for chunker in CHUNKERS:
        expected = len(chunk_documents(docs, chunker))
        for name in STORE_NAMES:
            if name not in available_stores(settings):
                table.add_row(name, chunker, str(expected), "not configured")
                continue
            try:
                backend = get_store(name, chunker, settings=settings)
                stored = str(backend.count())
                close = getattr(backend, "close", None)
                if callable(close):
                    close()
            except Exception as exc:
                stored = f"unreachable ({type(exc).__name__})"
            table.add_row(name, chunker, str(expected), stored)
    console.print(table)
