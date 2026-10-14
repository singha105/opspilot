"""Every relative link and image in the repository's Markdown points at a file that exists."""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def markdown_files() -> list[Path]:
    """The Markdown files under version control (local, untracked notes are not checked)."""
    out = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [ROOT / line for line in out.splitlines()]


def broken_links(path: Path) -> list[str]:
    text = re.sub(r"```.*?```", "", path.read_text(), flags=re.DOTALL)  # ignore code blocks
    text = re.sub(r"`[^`\n]*`", "", text)  # and inline code
    problems = []
    for target in LINK.findall(text):
        if re.match(r"^(https?:|mailto:|#)", target):
            continue
        file_part = target.split("#", 1)[0]
        if file_part and not (path.parent / file_part).exists():
            problems.append(target)
    return problems


@pytest.mark.parametrize("path", markdown_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(path: Path) -> None:
    assert broken_links(path) == []


def test_the_checker_finds_a_broken_link(tmp_path: Path) -> None:
    doc = tmp_path / "x.md"
    (tmp_path / "ok.md").write_text("")
    doc.write_text("[a](ok.md) [b](missing.md#top) [c](https://x.y) ![d](img.png) `[e](no.md)`")
    assert broken_links(doc) == ["missing.md#top", "img.png"]
