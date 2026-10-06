"""Chunkers: Markdown-section-aware (default) and fixed-size (baseline).

Token counts are estimated as ``ceil(chars / 4)``, a standard approximation for
English WordPiece/BPE tokenizers. It keeps chunking deterministic and free of model
downloads; at a 400-token target the chunks stay well under bge-small's 512-token
input limit even with the contextual header.
"""

import hashlib
import math
import re
from collections.abc import Callable, Sequence

from opspilot.rag.models import Chunk, ChunkerName, Document

CHARS_PER_TOKEN = 4
SECTION_TARGET_TOKENS = 400
SECTION_OVERLAP_TOKENS = 60
FIXED_CHARS = 800
FIXED_OVERLAP_CHARS = 100
SEPARATORS = ("\n\n", "\n", ". ", " ")

_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")

Chunker = Callable[[Document], list[Chunk]]


def estimate_tokens(text: str) -> int:
    """Approximate token count (``ceil(chars / 4)``)."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def chunk_id(doc_id: str, section: str, index: int) -> str:
    """Stable id: sha1 of doc id, section path and index."""
    return hashlib.sha1(f"{doc_id}\x1f{section}\x1f{index}".encode()).hexdigest()  # noqa: S324


def split_sections(body: str) -> list[tuple[list[str], str]]:
    """Split Markdown on H1-H3 headings (ignoring code fences) into (path, text) pairs."""
    sections: list[tuple[list[str], str]] = []
    levels: list[str | None] = [None, None, None]  # current H1, H2, H3
    lines: list[str] = []
    in_fence = False

    def flush() -> None:
        text = "\n".join(lines).strip()
        if text:
            sections.append(([h for h in levels if h], text))
        lines.clear()

    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else _HEADING.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            levels[level - 1] = heading.group(2).strip()
            levels[level:] = [None] * (3 - level)
        else:
            lines.append(line)
    flush()
    return sections


def split_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    """Recursively split ``text`` on paragraph, line, sentence then word boundaries.

    Pieces are at most ``max_chars`` long; consecutive pieces share roughly
    ``overlap_chars`` of trailing text from the previous piece.
    """
    if len(text) <= max_chars:
        return [text]
    units = _units(text, max_chars, SEPARATORS)
    pieces: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + len(unit) > max_chars:
            pieces.append(current.strip())
            current = _tail(current, overlap_chars)
        current += unit
    if current.strip():
        pieces.append(current.strip())
    return pieces


def _units(text: str, max_chars: int, separators: Sequence[str]) -> list[str]:
    """Break text into units no longer than ``max_chars``, keeping separators attached."""
    if len(text) <= max_chars:
        return [text]
    if not separators:
        return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]
    sep, rest = separators[0], separators[1:]
    parts = text.split(sep)
    units: list[str] = []
    for i, part in enumerate(parts):
        piece = part + (sep if i < len(parts) - 1 else "")
        units.extend(_units(piece, max_chars, rest))
    return [u for u in units if u]


def _tail(text: str, overlap_chars: int) -> str:
    """Last ``overlap_chars`` of text, starting at a word boundary."""
    if overlap_chars <= 0:
        return ""
    tail = text[-overlap_chars:]
    space = tail.find(" ")
    return tail[space + 1 :] if 0 <= space < len(tail) - 1 else tail


def markdown_section_chunks(doc: Document) -> list[Chunk]:
    """Section-aware chunks with a ``title > section path`` contextual header."""
    max_chars = SECTION_TARGET_TOKENS * CHARS_PER_TOKEN
    overlap = SECTION_OVERLAP_TOKENS * CHARS_PER_TOKEN
    chunks: list[Chunk] = []
    next_index: dict[str, int] = {}  # a repeated heading path continues its numbering
    for path, text in split_sections(doc.body):
        if path and path[0] == doc.title:
            path = path[1:]
        section = " > ".join(path)
        header = " > ".join([doc.title, *path])
        for piece in split_text(text, max_chars, overlap):
            index = next_index.get(section, 0)
            next_index[section] = index + 1
            chunks.append(_chunk(doc, section, index, f"{header}\n\n{piece}"))
    return chunks


def fixed_chunks(doc: Document) -> list[Chunk]:
    """Baseline: fixed 800-character windows with 100 characters of overlap."""
    body = doc.body.strip()
    step = FIXED_CHARS - FIXED_OVERLAP_CHARS
    starts = range(0, max(len(body) - FIXED_OVERLAP_CHARS, 1), step)
    return [_chunk(doc, "", i, body[s : s + FIXED_CHARS]) for i, s in enumerate(starts)]


def _chunk(doc: Document, section: str, index: int, text: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id(doc.doc_id, section, index),
        doc_id=doc.doc_id,
        doc_type=doc.doc_type,
        title=doc.title,
        section=section,
        index=index,
        text=text,
        categories=[c.value for c in doc.categories],
        services=list(doc.services),
    )


CHUNKERS: dict[ChunkerName, Chunker] = {
    "markdown_section": markdown_section_chunks,
    "fixed": fixed_chunks,
}


def chunk_documents(docs: Sequence[Document], chunker: ChunkerName) -> list[Chunk]:
    """Chunk every document with the named chunker."""
    fn = CHUNKERS[chunker]
    return [chunk for doc in docs for chunk in fn(doc)]
