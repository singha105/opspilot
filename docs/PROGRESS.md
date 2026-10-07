# Progress

## Day 3 — MCP servers and record/replay (2026-10-07)

**Done:**
- `mcp_servers/common`: explicit-kubeconfig clients, namespace allowlist, redaction (7
  pattern families), a 6,000-character cap with a truncation marker and hint, structured
  errors, 10 s timeouts, and a JSONL audit log (`runs/audit.jsonl`) for every call,
  including rejections.
- `opspilot-k8s`: 11 read-only tools with Pydantic output models and prompt-style
  descriptions (25–41 words each, every one with an example call). No write tool exists.
- `opspilot-kb`: `search_knowledge` and `get_document` over the Day 2 retriever (ADR-0004
  defaults), returning citation-ready chunks.
- `opspilot-actions`: `plan_action` (dryRun=All, diff, risk notes, action hash) and
  `execute_action` gated by HMAC approval tokens from `agent/approval.py` (≤ 10 min,
  single use, bound to the action hash). Five allowlisted actions with server-side bounds.
- Record/replay: `opspilot faults record <id> | --all` and `opspilot-k8s --mode replay`.
  Six fixtures were recorded from the cluster: the 5 scenarios plus a `healthy` baseline.
- `opspilot.tools`: LangChain tools through `langchain-mcp-adapters` (k8s + kb by default,
  actions only on request and never in replay), and `scripts/mcp_smoke.py`.
- Both read servers were verified with MCP Inspector 2.9.0 (CLI mode): tool list,
  calls, and a rejected out-of-range argument.

**Decisions:**
- [ADR-0006](adr/0006-mcp-server-split.md): three servers split by privilege.
- [ADR-0007](adr/0007-approval-tokens.md): single-use HMAC approval tokens and
  server-side bounds.
- [ADR-0008](adr/0008-record-replay-evals.md): exhaustive recording through the
  production tool path; replay with safe derivations only.

**Metrics:**
- Live k8s tool latency over 314 audited calls today: p50 4.3 ms, p95 87 ms (slowest:
  `list_pvcs`/`list_nodes` around 45 ms p50). Replayed calls: p50 0.1 ms.
- Output size: p50 662 characters, largest 5,985 (cap 6,000); 3 of 43 calls per fixture
  are truncated (long logs) and carry the narrowing hint.
- Fixtures: 39–43 calls and 61–67 KB each. Recording all six took 3 min 27 s.
- Tests: 203 unit tests passed, 81% line coverage of `src/opspilot` (`make test`). The
  integration tests for MCP live tools, 403 handling, a stdio session, actions planning
  and a token-gated execution all pass.

**Known issues / debt:**
- FastMCP validates argument types before our code runs, so bounds are advertised in
  the schema but enforced in the audited path (otherwise rejections went unlogged;
  found with the Inspector).
- kubernetes client 36: `read_namespaced_pod_log` returns a bytes repr unless called with
  `_preload_content=False`; exec probes are on `V1Probe._exec`; a dict PATCH body
  defaults to json-patch, so strategic-merge patches pass `_content_type` explicitly.
- `langchain-mcp-adapters` 0.3 returns tool content as a list of blocks; `tool_text()`
  normalizes it.
- The operator Role cannot read ReplicaSets, so rollback and image-history checks read
  through the reader identity. Neither Role was widened.
- The approver name in a token is self-asserted (no SSO on a single host).
- The healthy fixture contains older "Unhealthy" warning events from earlier tests.
  That is realistic noise and was kept.
- The package version stayed at 0.1.0 through the v0.2.0 tag; it is 0.3.0 from today.

**Next:**
- LangGraph agent: triage, retrieve, investigate, diagnose, propose, human approval
  (`interrupt()`), act, verify, report, with a SQLite checkpointer.


## Day 2 — Knowledge base and hybrid RAG (2026-10-05)

**Done:**
- Knowledge base of 52 documents: 22 runbooks (14 categories plus 8 distractors), 10
  postmortems (including two OOM incidents with different causes), 4 service cards and
  16 Kubernetes docs (CC BY 4.0, pinned commit, `ATTRIBUTION.md`).
  `scripts/validate_kb.py` runs in CI.
- The 5 Day 1 scenarios now name their runbooks in `expected.runbook_ids`.
- `opspilot.rag`: loader, `markdown_section` and `fixed` chunkers with stable ids,
  fastembed bge-small embeddings with an on-disk cache, Weaviate / Chroma / optional
  Pinecone stores behind one `VectorStore` protocol, weighted RRF, FlashRank reranker,
  and a retriever with multi-query fusion, dedupe, a per-document cap and citations.
- CLI: `opspilot kb ingest | search | stats`, `opspilot eval check-queries | retrieval`.
- 60-query retrieval eval with a title-leakage check, a 28-config matrix,
  `evals/retrieval/RESULTS.md`, a chart and raw JSON. CI runs a 15-query Chroma smoke eval.

**Decisions:**
- [ADR-0004](adr/0004-retrieval-defaults.md): default retrieval is Weaviate hybrid α=0.5 on
  section chunks without reranking (best nDCG@5 under 300 ms p95).
- [ADR-0005](adr/0005-vector-store-abstraction.md): one store protocol, identical
  vectors in every store, a collection per chunker, and RRF for stores without native hybrid.

**Metrics:**
- Ingest: 740 section chunks and 741 fixed chunks in both Weaviate and Chroma, counts
  matching (`opspilot kb ingest`). A full re-ingest of both chunkers into both stores
  takes about 40 s with a warm embedding cache.
- Retrieval (60 queries, `opspilot eval retrieval --configs all`):
  - Default config: nDCG@5 0.817, R@5 0.933, MRR@10 0.746, p95 42 ms.
  - Best overall, Chroma hybrid + rerank: nDCG@5 0.857, p95 1.9 s.
  - Dense only: nDCG@5 0.672.
- CI smoke baseline: Recall@5 1.000 on 15 queries (Chroma, hybrid, no rerank).
- Unit tests: 112 passed, 84% line coverage of `src/opspilot` (`make test`).
  Store contract suite: Chroma (unit) and Weaviate (integration) pass. Pinecone is
  skipped because no key is set.

**Known issues / debt:**
- Latency was measured while the laptop was swapping heavily (8–10 GB of swap in use;
  a Multipass VM held about 2.6 GB). Treat absolute p95 numbers as upper bounds.
- The first ingest attempt hung for over 10 minutes: fastembed's default batch of 256
  long chunks needed gigabytes of padded attention. Fixed with length-sorted batches of 16.
- Multipass's daemon owns host port 50051 (through launchd), so Weaviate gRPC is
  published on 50052. Weaviate HTTP stays on 8090.
- In the default config, a postmortem about the same incident sometimes outranks the
  runbook (3 of the 4 Recall@5 misses).
- Reranking costs 0.5–4.5 s p95 here. A smaller reranker or a shorter `max_length` is
  untested; added to the Day 5 ablations.
- Two smoke queries sit near the rank-5 cut-off (q016 at rank 5, q041 at rank 4). The
  subset was chosen before results and was kept. The CI run is the check that x86
  numerics agree.
- `kb stats` creates an empty Weaviate collection if one is missing.

**Next:**
- MCP servers (`k8s` read-only, `kb`, gated `actions`) and record/replay of tool calls.


## Day 1 — Foundation (2026-10-05)

**Done:**
- uv project (Python 3.12, src layout), settings, structlog JSON logging, Typer CLI
  (`opspilot --version`, `opspilot faults ...`), ruff, mypy strict, pytest,
  pre-commit and a self-documenting Makefile.
- `infra/docker-compose.yml`: Weaviate 1.39.8 with all modules disabled, plus Phoenix
  behind the `tracing` profile. k3d cluster `opspilot` (1 server; traefik,
  servicelb and metrics-server disabled).
- Shopfront demo in namespace `shop`: payments-api, orders-api, inventory-api and
  redis. All three app services run from one stdlib-only image whose behaviour
  comes from env vars.
- RBAC: `opspilot-reader` (read-only, no Secrets, no exec) and `opspilot-operator`
  (get/patch Deployments in `shop` only). Short-lived kubeconfigs go in `.secrets/`.
- Fault injection: scenario schema, admin-context injector, CLI, and 5 scenarios
  (`oom-payments`, `imagepull-orders-tag`, `missing-env-inventory`,
  `readiness-payments`, `redis-down`).
- GitHub Actions CI: ruff, ruff format, mypy, unit tests with coverage.

**Decisions:**
- [ADR-0001](adr/0001-local-first-zero-cost.md): local-first, $0 stack.
- [ADR-0002](adr/0002-read-only-by-default-agent-access.md): read-only by default
  agent access, enforced by RBAC.
- [ADR-0003](adr/0003-fault-injection-as-ground-truth.md): fault injection as ground
  truth, and the no-surge rollout for demo Deployments.

**Metrics:**
- Unit tests: 55 passed, 99% line coverage of `src/opspilot` (`make test`).
- Integration tests: 44 RBAC checks and 5 of 5 scenarios inject, show their symptom
  and reset (`make test-integration`).
- Time to symptom, measured by the integration test from inject until the expected
  pod reason or not-ready condition was observed (3 s poll): imagepull 3.3 s,
  oom 6.4 s, missing-env 6.9 s, readiness 12.9 s, redis-down 13.2 s. p50 = 6.9 s.
- Time to reset (until every Deployment in `shop` was ready again): p50 = 12.2 s,
  max 16.0 s.
- Idle memory: k3d node container about 640 MiB (`docker stats`). The `shop`
  namespace uses about 90 MiB in total (sum of pod cgroup `memory.current`).
  Weaviate idles at 49–140 MiB under its 700 MiB limit.
- Demo image: 148 MB uncompressed filesystem, 47 MB compressed.

**Known issues / debt:**
- Weaviate is published on host port 8090 rather than 8080, because 8080 was taken by
  a local web server on the dev machine.
- Weaviate 1.33+ ignores `ENABLE_API_BASED_MODULES`; modules are disabled with
  `API_BASED_MODULES_DISABLED=true` instead (library differs from the original notes).
- In kubernetes client 36 the bearer token is stored under `api_key["BearerToken"]`.
- `kubectl auth can-i get pods/exec` treats `exec` as a pod name, so the RBAC tests
  pass subresources through `--subresource`.
- ServiceAccount tokens expire after 12 h, so run `make kubeconfigs` in each new session.
- Scenario `runbook_ids` are empty until the knowledge base exists (Day 2).

**Next:**
- Knowledge base (runbooks, postmortems, service cards, Kubernetes docs) and the
  hybrid RAG pipeline over Weaviate, Chroma and optional Pinecone.
- Retrieval evals, and filling in `runbook_ids` for the 5 scenarios.
