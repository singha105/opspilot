import importlib.util
import shutil
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import ValidationError

from opspilot.rag.schema import DocMeta, is_document, split_front_matter

ROOT = Path(__file__).parents[2]


def _load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "validate_kb", ROOT / "scripts" / "validate_kb.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validate_kb = _load_validator()


def _meta(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "rb-example",
        "title": "Example runbook",
        "doc_type": "runbook",
        "categories": ["OOM_KILLED"],
        "services": ["payments-api"],
    }
    data.update(overrides)
    return data


def test_valid_meta() -> None:
    meta = DocMeta.model_validate(_meta())
    assert meta.license == "MIT"


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "runbook-example"},
        {"categories": ["NOT_A_CATEGORY"]},
        {"services": ["billing-api"]},
        {"doc_type": "wiki"},
        {"surprise": True},
        {"doc_type": "postmortem", "id": "pm-2026-01-x"},
        {"doc_type": "k8s_doc", "id": "k8s-x"},
    ],
)
def test_invalid_meta(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DocMeta.model_validate(_meta(**overrides))


def test_split_front_matter() -> None:
    data, body = split_front_matter("---\nid: rb-x\n---\n# Title\n")
    assert data == {"id": "rb-x"}
    assert body == "# Title\n"
    with pytest.raises(ValueError, match="front matter"):
        split_front_matter("# no front matter\n")


def test_is_document() -> None:
    assert is_document(Path("knowledge/runbooks/rb-oom-killed.md"))
    assert not is_document(Path("knowledge/k8s-docs/ATTRIBUTION.md"))


def test_repository_knowledge_base_is_valid() -> None:
    assert validate_kb.validate(ROOT / "knowledge", ROOT / "faults" / "scenarios") == []


def test_validator_reports_broken_links_and_ids(tmp_path: Path) -> None:
    knowledge = tmp_path / "knowledge"
    shutil.copytree(ROOT / "knowledge", knowledge)
    runbook = knowledge / "runbooks" / "rb-oom-killed.md"
    text = runbook.read_text().replace("related: [", "related: [rb-does-not-exist, ")
    runbook.write_text(text + "\n[gone](missing-file.md)\n")
    (knowledge / "runbooks" / "rb-copy.md").write_text(runbook.read_text())
    problems = validate_kb.validate(knowledge, ROOT / "faults" / "scenarios")
    joined = "\n".join(problems)
    assert "related id 'rb-does-not-exist' does not exist" in joined
    assert "broken link missing-file.md" in joined
    assert "does not match the file name" in joined
    assert "duplicate id rb-oom-killed" in joined
