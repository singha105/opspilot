# OpsPilot

[![CI](https://github.com/singha105/opspilot/actions/workflows/ci.yml/badge.svg)](https://github.com/singha105/opspilot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

OpsPilot is an incident-response agent for Kubernetes. Given an alert such as
"payments-api pods crashlooping in namespace shop", it triages the alert, retrieves
the relevant runbooks and postmortems with hybrid RAG, investigates the cluster
through a read-only MCP server, and produces a root-cause diagnosis that cites its
evidence. It then proposes a fix from a fixed allowlist, waits for a human to
approve it, applies only that action through a separate gated server, and checks
that the service recovered. A suite of fault-injection scenarios with known root
causes measures how often it gets the diagnosis right. Everything runs locally for
$0 on an 8 GB laptop.

## Planned architecture

- **Agent:** a LangGraph state machine (triage → retrieve → investigate → diagnose →
  propose → human approval → act → verify → report) with a SQLite checkpointer.
- **LLM:** `qwen3:4b` served locally by Ollama, temperature 0, behind a
  provider-agnostic factory.
- **Tools:** three MCP servers. `k8s` is read-only, `kb` serves the knowledge base,
  and `actions` executes approved remediations with single-use approval tokens.
- **Retrieval:** hybrid search (BM25 + `bge-small-en-v1.5` vectors, fused with
  Reciprocal Rank Fusion, FlashRank reranking) over Weaviate, with Chroma and
  Pinecone behind the same interface for comparison.
- **Safety:** the agent's ServiceAccount cannot read Secrets, exec into pods or
  write anything. Logs, events and documents are treated as untrusted data.
- **Evals:** 30 fault scenarios injected into a demo app, scored on root-cause
  accuracy, evidence quality and remediation choice.

## Status: Day 2 of 6

| Area | State |
|---|---|
| Tooling, Makefile, CI | done |
| Local k3d cluster + Weaviate | done |
| Shopfront demo app (4 services) | done |
| Read-only reader / narrow operator RBAC | done, proven by tests |
| Fault injection framework | done, 5 of 30 scenarios |
| Knowledge base (52 docs) + hybrid RAG | done, measured on 60 queries |
| MCP servers | Day 3 |
| LangGraph agent + human approval | Day 4 |
| Full eval suite + safety tests | Day 5 |
| API, UI, docs, v1.0.0 | Day 6 |

**Retrieval, measured.** On 60 graded queries, the default (Weaviate hybrid search over
section-aware chunks) reaches nDCG@5 0.817 and Recall@5 0.933 at 42 ms p95. Adding
FlashRank reranking raises nDCG@5 to 0.852, but costs over a second per query on this
laptop, so it is opt-in. Dense-only search scores 0.672. Details in
[docs/rag.md](docs/rag.md) and [ADR-0004](docs/adr/0004-retrieval-defaults.md).

Progress notes: [docs/PROGRESS.md](docs/PROGRESS.md). Design decisions:
[docs/adr/](docs/adr/).

## Quickstart

Prerequisites: macOS or Linux, Docker (OrbStack or Docker Desktop), `k3d`, `kubectl`,
`uv` and `make`.

```bash
make setup                       # uv sync + pre-commit hook
make lint typecheck test         # unit tests need no cluster

make cluster-up                  # single-node k3d cluster "opspilot"
make demo-build demo-deploy      # build the demo image, deploy Shopfront to namespace shop
make rbac-apply                  # reader/operator ServiceAccounts + kubeconfigs in .secrets/
make test-integration            # RBAC proofs + every fault scenario end to end

make fault-list
make fault-inject ID=oom-payments
make fault-status
make fault-reset ID=oom-payments

make infra-up                    # Weaviate (HTTP 8090, gRPC 50052)
uv run opspilot kb ingest        # chunk, embed and index the knowledge base
uv run opspilot kb search "payments pods restart with exit code 137"
uv run opspilot eval retrieval --configs all

make down-all                    # stop the cluster, containers and the Ollama model
```

`make help` lists every target.

## Repository layout

```
src/opspilot/   package: config, logging, CLI, models, faults, rag, evals (agent, MCP to come)
demo/           Shopfront demo service (app/) and kustomize manifests (k8s/)
faults/         fault scenario definitions with ground-truth root causes
knowledge/      runbooks, postmortems, service cards and Kubernetes docs
evals/          retrieval queries, results and reports
infra/          docker-compose (Weaviate, Phoenix), k3d cluster config, RBAC
docs/           progress log and architecture decision records
tests/          unit tests (no cluster) and integration tests (cluster)
```

## License

MIT, see [LICENSE](LICENSE).
