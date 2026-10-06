from pathlib import Path

import pytest

from opspilot.rag import chunking
from opspilot.rag.loader import load_document, load_documents
from opspilot.rag.models import Document

KNOWLEDGE = Path(__file__).parents[2] / "knowledge"


def doc(body: str, doc_id: str = "rb-test", title: str = "Test runbook") -> Document:
    return Document(
        doc_id=doc_id,
        doc_type="runbook",
        title=title,
        source_path="knowledge/runbooks/rb-test.md",
        license="MIT",
        body=body,
    )


def test_split_sections_tracks_heading_path_and_ignores_fences() -> None:
    body = (
        "# Title\nintro\n## A\ntext a\n```bash\n# not a heading\n```\n"
        "### A1\ntext a1\n## B\ntext b\n#### deep stays in B\n"
    )
    sections = chunking.split_sections(body)
    assert [path for path, _ in sections] == [
        ["Title"],
        ["Title", "A"],
        ["Title", "A", "A1"],
        ["Title", "B"],
    ]
    assert "# not a heading" in sections[1][1]
    assert "#### deep stays in B" in sections[3][1]


def test_markdown_section_chunks_have_contextual_header() -> None:
    chunks = chunking.markdown_section_chunks(doc("# Test runbook\n## Symptoms\npods restart\n"))
    assert len(chunks) == 1
    assert chunks[0].section == "Symptoms"
    assert chunks[0].text == "Test runbook > Symptoms\n\npods restart"


def test_long_section_is_split_with_overlap_under_the_limit() -> None:
    sentence = "The payments pod restarts because memory grows. "
    chunks = chunking.markdown_section_chunks(doc("## Long\n" + sentence * 200))
    assert len(chunks) > 1
    limit = chunking.SECTION_TARGET_TOKENS * chunking.CHARS_PER_TOKEN
    header = len("Test runbook > Long\n\n")
    assert all(len(c.text) <= limit + header for c in chunks)
    first_tail = chunks[0].text[-80:]
    assert first_tail.split(" ", 1)[1][:30] in chunks[1].text


def test_chunk_ids_are_deterministic_and_unique() -> None:
    docs = load_documents(KNOWLEDGE)
    for name in ("markdown_section", "fixed"):
        first = chunking.chunk_documents(docs, name)  # type: ignore[arg-type]
        second = chunking.chunk_documents(docs, name)  # type: ignore[arg-type]
        assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
        assert len({c.chunk_id for c in first}) == len(first)


def test_chunk_id_formula() -> None:
    import hashlib

    expected = hashlib.sha1(b"rb-x\x1fSymptoms\x1f0").hexdigest()
    assert chunking.chunk_id("rb-x", "Symptoms", 0) == expected


def test_repeated_section_path_continues_numbering() -> None:
    chunks = chunking.markdown_section_chunks(doc("## Example\none\n## Example\ntwo\n"))
    assert [c.index for c in chunks] == [0, 1]
    assert chunks[0].chunk_id != chunks[1].chunk_id


def test_fixed_chunks_size_and_overlap() -> None:
    body = "".join(f"{i:04d}" for i in range(500))  # 2000 chars, no spaces
    chunks = chunking.fixed_chunks(doc(body))
    assert all(len(c.text) <= chunking.FIXED_CHARS for c in chunks)
    assert (
        chunks[0].text[-chunking.FIXED_OVERLAP_CHARS :]
        == chunks[1].text[: chunking.FIXED_OVERLAP_CHARS]
    )
    assert "".join(c.text for c in chunks).replace(chunks[1].text[:100], "", 1)
    assert chunks[-1].text.endswith(body[-20:])


def test_split_text_falls_back_to_hard_splits() -> None:
    pieces = chunking.split_text("x" * 50, max_chars=20, overlap_chars=0)
    assert pieces == ["x" * 20, "x" * 20, "x" * 10]


def test_estimate_tokens() -> None:
    assert chunking.estimate_tokens("abcd" * 10) == 10
    assert chunking.estimate_tokens("abcde") == 2


def test_loader_reads_repository_corpus() -> None:
    docs = load_documents(KNOWLEDGE)
    types = {d.doc_type for d in docs}
    assert types == {"runbook", "postmortem", "service_card", "k8s_doc"}
    assert len(docs) == 52
    oom = next(d for d in docs if d.doc_id == "rb-oom-killed")
    assert oom.categories[0].value == "OOM_KILLED"
    assert oom.source_path == "knowledge/runbooks/rb-oom-killed.md"


def test_loader_rejects_bad_documents(tmp_path: Path) -> None:
    bad = tmp_path / "rb-bad.md"
    bad.write_text("---\nid: rb-other\ntitle: Bad doc\ndoc_type: runbook\n---\nbody\n")
    with pytest.raises(ValueError, match="does not match"):
        load_document(bad)
    bad.write_text("no front matter")
    with pytest.raises(ValueError, match="front matter"):
        load_document(bad)
