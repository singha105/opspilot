"""Validate the knowledge base: schema, ids, categories, links and coverage.

Usage: uv run python scripts/validate_kb.py [knowledge_dir] [scenarios_dir]
Exits non-zero and lists every problem when anything is wrong.
"""

import re
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

from opspilot.models import RootCauseCategory
from opspilot.rag.schema import DIRECTORIES, DocMeta, is_document, split_front_matter

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK_SECTIONS = [
    "Symptoms",
    "Quick checks (read-only)",
    "Diagnosis decision tree",
    "Remediation options",
    "Do NOT",
    "Verify recovery",
    "Escalation",
    "Related",
]
RUNBOOK_WORDS = (300, 900)
LINK = re.compile(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)")


def validate(knowledge: Path, scenarios: Path) -> list[str]:
    """Return a list of human-readable problems (empty when the KB is valid)."""
    problems: list[str] = []
    docs: dict[str, tuple[Path, DocMeta, str]] = {}

    for path in sorted(knowledge.rglob("*.md")):
        if not is_document(path):
            continue
        rel = path.relative_to(knowledge)
        try:
            data, body = split_front_matter(path.read_text())
            meta = DocMeta.model_validate(data)
        except (ValueError, ValidationError) as exc:
            problems.append(f"{rel}: {exc}")
            continue
        if meta.id != path.stem:
            problems.append(f"{rel}: id {meta.id!r} does not match the file name")
        if rel.parts[0] != DIRECTORIES[meta.doc_type]:
            problems.append(
                f"{rel}: doc_type {meta.doc_type} belongs in {DIRECTORIES[meta.doc_type]}/"
            )
        if meta.id in docs:
            problems.append(f"{rel}: duplicate id {meta.id} (also {docs[meta.id][0]})")
        docs[meta.id] = (path, meta, body)

    for doc_id, (path, meta, body) in docs.items():
        rel = path.relative_to(knowledge)
        for target in meta.related:
            if target not in docs:
                problems.append(f"{rel}: related id {target!r} does not exist")
        for link in LINK.findall(body):
            if link.startswith(("http://", "https://")):
                continue
            if not (path.parent / link).resolve().is_file():
                problems.append(f"{rel}: broken link {link}")
        if meta.doc_type == "runbook":
            words = len(body.split())
            low, high = RUNBOOK_WORDS
            if not low <= words <= high:
                problems.append(f"{rel}: {words} words, expected {low}-{high}")
            headings = re.findall(r"^## (.+)$", body, re.MULTILINE)
            if headings != RUNBOOK_SECTIONS:
                problems.append(f"{rel}: sections {headings} do not follow the runbook template")
        if meta.doc_type == "k8s_doc":
            attribution = (path.parent / "ATTRIBUTION.md").read_text()
            if doc_id not in attribution:
                problems.append(f"{rel}: missing from ATTRIBUTION.md")

    covered = {c for _, m, _ in docs.values() if m.doc_type == "runbook" for c in m.categories}
    for category in RootCauseCategory:
        if category is not RootCauseCategory.UNKNOWN and category not in covered:
            problems.append(f"no runbook covers category {category}")

    for path in sorted(scenarios.glob("*.yaml")):
        scenario = yaml.safe_load(path.read_text())
        for runbook_id in scenario["expected"].get("runbook_ids", []):
            if runbook_id not in docs or docs[runbook_id][1].doc_type != "runbook":
                problems.append(f"{path.name}: runbook id {runbook_id!r} does not exist")
        if not scenario["expected"].get("runbook_ids"):
            problems.append(f"{path.name}: expected.runbook_ids is empty")

    return problems


def main() -> int:
    knowledge = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "knowledge"
    scenarios = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "faults" / "scenarios"
    problems = validate(knowledge, scenarios)
    for problem in problems:
        print(f"ERROR {problem}")
    if problems:
        print(f"{len(problems)} problem(s) found")
        return 1
    counts: dict[str, int] = {}
    for path in knowledge.rglob("*.md"):
        if is_document(path):
            counts[path.parent.name] = counts.get(path.parent.name, 0) + 1
    print("knowledge base OK: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
