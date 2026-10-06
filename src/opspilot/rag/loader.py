"""Load knowledge-base Markdown files into :class:`Document` models."""

from pathlib import Path

from opspilot.rag.models import Document
from opspilot.rag.schema import DocMeta, is_document, split_front_matter


def load_document(path: Path, root: Path | None = None) -> Document:
    """Parse and validate one document; raises ``ValueError`` on bad front matter."""
    data, body = split_front_matter(path.read_text())
    meta = DocMeta.model_validate(data)
    if meta.id != path.stem:
        raise ValueError(f"{path.name}: id {meta.id!r} does not match the file name")
    source = path.relative_to(root) if root else path
    return Document(
        doc_id=meta.id,
        doc_type=meta.doc_type,
        title=meta.title,
        categories=list(meta.categories),
        services=list(meta.services),
        tags=list(meta.tags),
        source_path=str(source),
        license=meta.license,
        body=body,
    )


def load_documents(knowledge_dir: Path) -> list[Document]:
    """Load every document under ``knowledge_dir``, sorted by id."""
    paths = sorted(p for p in knowledge_dir.rglob("*.md") if is_document(p))
    docs = [load_document(path, knowledge_dir.parent) for path in paths]
    seen: set[str] = set()
    for doc in docs:
        if doc.doc_id in seen:
            raise ValueError(f"duplicate document id {doc.doc_id}")
        seen.add(doc.doc_id)
    return sorted(docs, key=lambda d: d.doc_id)
