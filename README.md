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

## Status: Day 4 of 6

| Area | State |
|---|---|
| Tooling, Makefile, CI | done |
| Local k3d cluster + Weaviate | done |
| Shopfront demo app (4 services) | done |
| Read-only reader / narrow operator RBAC | done, proven by tests |
| Fault injection framework | done, 5 of 30 scenarios |
| Knowledge base (52 docs) + hybrid RAG | done, measured on 60 queries |
| MCP servers (k8s read-only, kb, gated actions) | done, with approval tokens |
| Record/replay fixtures | done, 6 fixtures |
| LangGraph agent + human approval | done, one live fix approved end to end |
| Full eval suite + safety tests | Day 5 |
| API, UI, docs, v1.0.0 | Day 6 |

**Retrieval, measured.** On 60 graded queries, the default (Weaviate hybrid search over
section-aware chunks) reaches nDCG@5 0.817 and Recall@5 0.933 at 42 ms p95. Adding
FlashRank reranking raises nDCG@5 to 0.852, but costs over a second per query on this
laptop, so it is opt-in. Dense-only search scores 0.672. Details in
[docs/rag.md](docs/rag.md) and [ADR-0004](docs/adr/0004-retrieval-defaults.md).

**Tools, gated.** The agent sees the cluster through 11 read-only MCP tools and the
knowledge base through 2 more. Changing anything means calling a separate actions server
that dry-runs the change and executes it only with a human-approved, single-use token
bound to that exact action. Every call is redacted, size-capped and audited. Six recorded
fixtures let agent evals replay real incidents without a cluster. See
[docs/mcp.md](docs/mcp.md) and [docs/security.md](docs/security.md).

**Agent, approved by a human.** A LangGraph state machine triages the alert, retrieves
runbooks, investigates with up to 8 read-only tool calls, and writes a diagnosis that
must cite its evidence. Any change pauses the run until a person approves, rejects or
edits it, possibly from another terminal; only then is a single-use token minted. On the
six recorded fixtures it named the right category for all five faults (component right
in four), escalated the healthy false alarm, and never sent a wrong action to approval,
in 102-152 s per run on `qwen3:4b`. One live run fixed an OOM-killed service after
approval. See [docs/agent.md](docs/agent.md) and ADRs
[0009](docs/adr/0009-state-machine-over-free-form-react.md)-[0011](docs/adr/0011-small-model-strategies.md).

Progress notes: [docs/PROGRESS.md](docs/PROGRESS.md). Design decisions:
[docs/adr/](docs/adr/).

## Results

Measured on 20 held-out fault scenarios replayed from recordings, with `qwen3:4b` on an
8 GB laptop. Full report, ablations, safety and failure analysis:
[evals/REPORT.md](evals/REPORT.md); method: [docs/evals.md](docs/evals.md).

<!-- BEGIN results -->
| Metric (test split, n=20, config C3) | Value | 95% CI |
|---|---|---|
| Root-cause category accuracy | 12/20 (60%) | 39%-78% |
| Component (Deployment) correct | 19/20 (95%) | 76%-99% |
| Expected runbook retrieved (6 + up to 3 chunks) | 9/20 (45%) | 26%-66% |
| Expected runbook cited | 7/20 (35%) | 18%-57% |
| Remediation acceptable | 14/20 (70%) | 48%-85% |
| Citation validity | 0.99 mean; 19/20 runs fully valid | — |
| Prompt injections followed (all splits) | 0/2 | — |
| Unapproved action attempts (all 31 runs) | 0 | — |
| Median latency / tokens in / tokens out per incident | 125 s / 35,629 / 986 | — |
<!-- END results -->

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

uv run opspilot faults record --all            # record replay fixtures (cluster needed)
uv run python scripts/mcp_smoke.py --mode replay --fixture evals/fixtures/oom-payments.json
npx @modelcontextprotocol/inspector uv run opspilot-k8s

ollama pull qwen3:4b             # local model (~2.5 GB)
uv run opspilot investigate --scenario oom-payments --mode replay   # no cluster needed
uv run opspilot investigate --scenario oom-payments --mode live     # pauses for approval
uv run opspilot resume <incident-id>                                # approve / reject / edit
uv run opspilot runs list

make down-all                    # stop the cluster, containers and the Ollama model
```

`make help` lists every target.

## Repository layout

```
src/opspilot/   package: config, logging, CLI, models, faults, rag, evals, mcp_servers, tools
demo/           Shopfront demo service (app/) and kustomize manifests (k8s/)
faults/         fault scenario definitions with ground-truth root causes
knowledge/      runbooks, postmortems, service cards and Kubernetes docs
evals/          retrieval queries and results, recorded MCP fixtures
infra/          docker-compose (Weaviate, Phoenix), k3d cluster config, RBAC
docs/           progress log and architecture decision records
tests/          unit tests (no cluster) and integration tests (cluster)
```

## License

MIT, see [LICENSE](LICENSE).
