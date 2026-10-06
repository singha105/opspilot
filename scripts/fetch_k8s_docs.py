"""Fetch the Kubernetes documentation pages used in the knowledge base.

Pages come from the kubernetes/website repository (CC BY 4.0) at a pinned commit,
so the corpus is reproducible. Hugo shortcodes are stripped to plain Markdown and
site-relative links are made absolute. Re-run with:

    uv run python scripts/fetch_k8s_docs.py
"""

import re
import sys
from pathlib import Path

import httpx
import yaml

COMMIT = "abc9ea495989b5660aae4bb7dafa1fb15cad0aad"
RAW = f"https://raw.githubusercontent.com/kubernetes/website/{COMMIT}/content/en/docs"
BLOB = f"https://github.com/kubernetes/website/blob/{COMMIT}/content/en/docs"
SITE = "https://kubernetes.io/docs"
OUT = Path(__file__).resolve().parents[1] / "knowledge" / "k8s-docs"

# (doc id slug, path under content/en/docs, categories, tags)
PAGES: list[tuple[str, str, list[str], list[str]]] = [
    ("debug-pods", "tasks/debug/debug-application/debug-pods", [], ["debugging", "pods"]),
    (
        "debug-service",
        "tasks/debug/debug-application/debug-service",
        ["SERVICE_MISCONFIG", "READINESS_PROBE_MISCONFIG"],
        ["debugging", "service", "endpoints", "dns"],
    ),
    (
        "debug-running-pod",
        "tasks/debug/debug-application/debug-running-pod",
        [],
        ["debugging", "logs", "ephemeral-containers"],
    ),
    (
        "determine-reason-pod-failure",
        "tasks/debug/debug-application/determine-reason-pod-failure",
        ["BAD_COMMAND"],
        ["termination-message", "crashloop"],
    ),
    (
        "init-containers",
        "concepts/workloads/pods/init-containers",
        ["INIT_CONTAINER_FAILURE"],
        ["init-containers", "startup"],
    ),
    (
        "probes",
        "tasks/configure-pod-container/configure-liveness-readiness-startup-probes",
        ["LIVENESS_PROBE_MISCONFIG", "READINESS_PROBE_MISCONFIG"],
        ["probes", "liveness", "readiness", "startup"],
    ),
    (
        "manage-resources-containers",
        "concepts/configuration/manage-resources-containers",
        ["OOM_KILLED", "INSUFFICIENT_RESOURCES"],
        ["requests", "limits", "memory", "cpu"],
    ),
    (
        "assign-memory-resource",
        "tasks/configure-pod-container/assign-memory-resource",
        ["OOM_KILLED"],
        ["memory", "oomkilled", "limits"],
    ),
    ("images", "concepts/containers/images", ["IMAGE_PULL_ERROR"], ["images", "pull-policy"]),
    (
        "assign-pod-node",
        "concepts/scheduling-eviction/assign-pod-node",
        ["SCHEDULING_CONSTRAINT"],
        ["scheduling", "node-selector", "affinity"],
    ),
    (
        "taint-and-toleration",
        "concepts/scheduling-eviction/taint-and-toleration",
        ["SCHEDULING_CONSTRAINT"],
        ["scheduling", "taints", "tolerations"],
    ),
    (
        "persistent-volumes",
        "concepts/storage/persistent-volumes",
        ["PVC_PENDING"],
        ["storage", "pvc", "storageclass"],
    ),
    (
        "pod-lifecycle",
        "concepts/workloads/pods/pod-lifecycle",
        ["BAD_COMMAND", "LIVENESS_PROBE_MISCONFIG"],
        ["pod-phase", "restart-policy", "crashloop"],
    ),
    (
        "deployment",
        "concepts/workloads/controllers/deployment",
        ["BAD_ROLLOUT"],
        ["deployment", "rollout", "rollback"],
    ),
    (
        "configure-pod-configmap",
        "tasks/configure-pod-container/configure-pod-configmap",
        ["MISSING_CONFIGMAP_OR_SECRET", "CONFIG_MISSING_ENV"],
        ["configmap", "env"],
    ),
    (
        "dns-debugging-resolution",
        "tasks/administer-cluster/dns-debugging-resolution",
        [],
        ["dns", "coredns"],
    ),
]

FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
SHORTCODE_PAIRS = [
    # glossary tooltips keep their visible text
    (re.compile(r"\{\{<\s*glossary_tooltip\s+[^>]*?text=\"([^\"]+)\"[^>]*>\}\}"), r"\1"),
    (re.compile(r"\{\{%\s*heading\s+\"prerequisites\"\s*%\}\}"), "Before you begin"),
    (re.compile(r"\{\{%\s*heading\s+\"whatsnext\"\s*%\}\}"), "What's next"),
    (re.compile(r"\{\{%\s*heading\s+\"objectives\"\s*%\}\}"), "Objectives"),
    (re.compile(r"\{\{%\s*heading\s+\"cleanup\"\s*%\}\}"), "Clean up"),
    (re.compile(r"\{\{%\s*heading\s+\"synopsis\"\s*%\}\}"), "Synopsis"),
    (
        re.compile(r"\{\{[<%]\s*(?:code_sample|codenew)\s+[^}]*?file=\"([^\"]+)\"[^}]*[>%]\}\}"),
        r"(Example manifest: https://k8s.io/examples/\1)",
    ),
    (re.compile(r"\{\{[<%]\s*/?\s*(note|caution|warning)\s*[>%]\}\}"), ""),
]
ANY_SHORTCODE = re.compile(r"\{\{[<%].*?[>%]\}\}", re.DOTALL)
COMMENT_BLOCK = re.compile(r"\{\{<\s*comment\s*>\}\}.*?\{\{<\s*/comment\s*>\}\}", re.DOTALL)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
SITE_LINK = re.compile(r"\]\((/docs/[^)\s]*)\)")
BLANKS = re.compile(r"\n{3,}")


def clean(body: str) -> str:
    """Strip Hugo-specific markup so the page is plain Markdown."""
    body = COMMENT_BLOCK.sub("", body)
    body = HTML_COMMENT.sub("", body)
    for pattern, replacement in SHORTCODE_PAIRS:
        body = pattern.sub(replacement, body)
    body = ANY_SHORTCODE.sub("", body)
    body = SITE_LINK.sub(lambda m: f"](https://kubernetes.io{m.group(1)})", body)
    body = "\n".join(line.rstrip() for line in body.splitlines())
    return BLANKS.sub("\n\n", body).strip() + "\n"


def build(slug: str, path: str, categories: list[str], tags: list[str], raw: str) -> str:
    """Return the knowledge-base Markdown file for one page."""
    match = FRONT_MATTER.match(raw)
    if not match:
        raise ValueError(f"{path}: no front matter")
    hugo = yaml.safe_load(match.group(1))
    title = str(hugo["title"]).strip()
    meta = {
        "id": f"k8s-{slug}",
        "title": title,
        "doc_type": "k8s_doc",
        "categories": categories,
        "services": [],
        "tags": tags,
        "source_url": f"{SITE}/{path}/",
        "source_file": f"{BLOB}/{path}.md",
        "license": "CC-BY-4.0",
    }
    header = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000)
    return f"---\n{header}---\n# {title}\n\n{clean(raw[match.end() :])}"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=30) as client:
        for slug, path, categories, tags in PAGES:
            response = client.get(f"{RAW}/{path}.md")
            response.raise_for_status()
            text = build(slug, path, categories, tags, response.text)
            (OUT / f"k8s-{slug}.md").write_text(text)
            print(f"k8s-{slug}: {len(text.split())} words")
    return 0


if __name__ == "__main__":
    sys.exit(main())
