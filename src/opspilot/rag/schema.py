"""Front-matter schema for knowledge-base documents."""

import datetime as dt
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from opspilot.models import RootCauseCategory

DocType = Literal["runbook", "postmortem", "service_card", "k8s_doc"]

ID_PATTERNS: dict[str, str] = {
    "runbook": r"^rb-[a-z0-9]+(-[a-z0-9]+)*$",
    "postmortem": r"^pm-\d{4}-\d{2}-[a-z0-9]+(-[a-z0-9]+)*$",
    "service_card": r"^svc-[a-z0-9]+(-[a-z0-9]+)*$",
    "k8s_doc": r"^k8s-[a-z0-9]+(-[a-z0-9]+)*$",
}
DIRECTORIES: dict[str, str] = {
    "runbook": "runbooks",
    "postmortem": "postmortems",
    "service_card": "services",
    "k8s_doc": "k8s-docs",
}
SHOPFRONT_SERVICES = frozenset({"payments-api", "orders-api", "inventory-api", "redis"})

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


class DocMeta(BaseModel):
    """Validated front matter of one knowledge-base document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str = Field(min_length=3)
    doc_type: DocType
    categories: list[RootCauseCategory] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)
    severity: str | None = None
    last_reviewed: dt.date | None = None
    date: dt.date | None = None
    owner_team: str | None = None
    source_url: str | None = None
    source_file: str | None = None
    license: str = "MIT"

    @model_validator(mode="after")
    def _check(self) -> "DocMeta":
        if not re.match(ID_PATTERNS[self.doc_type], self.id):
            raise ValueError(f"id {self.id!r} does not follow the {self.doc_type} convention")
        unknown = set(self.services) - SHOPFRONT_SERVICES
        if unknown:
            raise ValueError(f"unknown services: {sorted(unknown)}")
        if self.doc_type == "k8s_doc" and (self.license != "CC-BY-4.0" or not self.source_url):
            raise ValueError("Kubernetes docs need license CC-BY-4.0 and a source_url")
        if self.doc_type == "postmortem" and self.date is None:
            raise ValueError("postmortems need a date")
        return self


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Split a Markdown file into its YAML front matter and body."""
    match = _FRONT_MATTER.match(text)
    if not match:
        raise ValueError("missing YAML front matter")
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("front matter must be a mapping")
    return data, text[match.end() :]


def is_document(path: Path) -> bool:
    """True for knowledge-base documents (skips ATTRIBUTION.md and similar files)."""
    return path.suffix == ".md" and path.name[:3] in {"rb-", "pm-", "svc", "k8s"}
