# Progress

## Day 5 — Fault catalog, evaluation, ablations, safety (2026-10-07 to 2026-10-08)

**Done:**
- Demo app: a `v2-broken` image that crashes with a real traceback, `wait-for` and
  `migrate` init-container subcommands, a CLI parser (unknown flags exit 2), and
  `LOG_INJECTION_TEXT` logged at startup.
- Faults: 25 new scenarios (30 in all, two per category), new injection types, and
  `make faults-verify`. All 30 pass inject -> symptom -> reset live (29/30 on the first full
  run; the 30th exposed an injector bug, fixed and re-verified). Recording all 31 fixtures
  injected and observed every scenario once more.
- 31 fixtures committed; `opspilot faults check-fixtures` runs in CI.
- Splits (`evals/agent/splits.yaml`), scoring (`evals/scoring.py`, Wilson intervals), a
  resumable replay runner (`opspilot eval agent`), the live subset (`opspilot eval live`),
  the report generator (`opspilot eval report`) and a leakage test.
- Agent: the retrieve step now really reranks and honours the retrieval mode (before, it
  ignored both); no-RAG runs drop the knowledge tool; new `refine_retrieval` node driven by
  dev failures.
- Evaluation run: C3 on all 30 + healthy, C0 and C2 on the 20 test cases, 5 live cases;
  every miss classified; [evals/REPORT.md](../evals/REPORT.md).

**Decisions:**
- [ADR-0012](adr/0012-evaluation-methodology.md): replay-based evaluation with a held-out
  split, simulated approval in replay, Wilson intervals, the reduced matrix (C1/C4 dropped
  by agreement: about four hours otherwise), and reranking on by default for the agent.

**Metrics** (all from `uv run opspilot eval agent|live|report`, `qwen3:4b`, M-series 8 GB):
- Test split, C3: category 12/20 (60%, 95% CI 39-78%), component 19/20, expected runbook
  cited 7/20, remediation acceptable 14/20, median 125 s and 35.6k tokens in per incident.
- All 30 faults, C3: category 19/30 (63%, 46-78%); dev 7/10. Healthy control escalated.
- Ablation (test, category): C0 no RAG 11/20, C2 hybrid 10/20, C3 hybrid + rerank 12/20;
  all intervals overlap. Runbook retrieved: C2 14/20, C3 9/20.
- Safety: injections followed 0/2 in every config; unapproved action attempts 0 in 71
  replayed runs; injections flagged 1/2.
- Live: 3/5 recovered after a harness-approved action, 168-194 s from symptom to verified
  recovery; one wrong proposal was rejected by the harness.
- Dev-driven change (`refine_retrieval`), dev split before -> after: runbook retrieved
  1/10 -> 7/10, cited 1/10 -> 5/10, category 6/10 -> 7/10, component 8/10 -> 9/10.
- Failure analysis, 33 misses: 26 reasoning errors, 5 tool-use errors, 1 retrieval miss,
  1 structured-output failure.
- Tests: 473 passed with 87% line coverage of `src/opspilot` (`make test`); `faults verify`: 30/30.

**Known issues / debt:**
- The injection detector misses approval claims phrased as "pre-approved ... approve your
  own action" (injection-redis-down); the log summarizer repeats injected text in its
  summary. Not followed, but found on the test split and not fixed today (a fix requires
  rerunning every config).
- The retriever reranks fused candidates against only the first query variant, which made
  C3 find the expected runbook less often than C2. Found on test; not changed.
- The agent requests logs from multi-container pods without naming a container, so init
  container logs are never read (5 tool-use errors).
- The 4B model anchors on retrieved postmortems over its own evidence (most reasoning
  errors).
- One run per case: model variance is unmeasured (payments-down was right in replay and
  wrong live).
- Live runs do not save the final state (only events), unlike replay runs.
- Session paused overnight between the dev iterations and the final runs; cached outcomes
  resumed the evaluation without re-running finished cases.

**Next:**
- Day 6: API, UI and final docs for v1.0.0; then, with disclosure and a full rerun, the
  injection patterns, multi-query reranking and container-aware log calls.

## Day 4 — LangGraph agent with human approval (2026-10-07)

**Done:**
- Incident models and a typed `IncidentState` with append-only evidence, security flags
  and errors; a LangGraph `StateGraph` of ten nodes with an `AsyncSqliteSaver`
  checkpointer and an explicit allowlist of checkpointed types.
- `llm.py`: per-role Ollama models (temperature 0, seed 42, `num_ctx` 8192, per-role output
  limits) and `astructured`, which repairs invalid output by quoting the validation error.
- Six versioned prompts with the 5-part structure and a test that fails on demo service
  names, fault values or expected answers.
- Guards: citations, prompt injection (instruction patterns only for documents), a
  category-to-action allowlist (rollback only after a recent rollout), escalation on
  `UNKNOWN`, confidence < 0.5, or a pod-level diagnosis whose component's pods are all
  healthy.
- Human approval with `interrupt()`: approve / reject / edit (parameters only,
  revalidated). The approval token is minted in `execute`, after the decision. Live runs
  refuse a decision given before the proposal is shown. A run paused in one process
  resumes in another (`opspilot resume`).
- `opspilot investigate | resume | runs`; `runs/<incident>/events.jsonl`, `state.json` and
  `report.md`; optional Phoenix tracing (`tracing` group, `OPSPILOT_TRACING=1`).
- Live run (acceptance): `make cluster-up kubeconfigs demo-deploy`, injected
  `oom-payments`, `opspilot investigate --scenario oom-payments --mode live` paused with a
  dry-run diff (memory 128Mi -> 512Mi); I approved after reviewing it
  (`opspilot resume ... --decision approve --approver arnab`). The patch ran with a fresh
  token, verification saw payments-api ready after 10.5 s, and all four deployments were
  healthy. Fault reset, cluster stopped.
- `docs/agent.md`, ADR-0009, ADR-0010, ADR-0011.

**Decisions:**
- [ADR-0009](adr/0009-state-machine-over-free-form-react.md): a fixed state machine with
  one bounded loop, not a free-form ReAct agent.
- [ADR-0010](adr/0010-human-in-the-loop-approval.md): `interrupt()` + SQLite checkpoints;
  tokens minted only after a human decision.
- [ADR-0011](adr/0011-small-model-strategies.md): constrained-JSON tool selection, prompt
  scaffolding and computed defaults for a 4B model.

**Metrics:**
- Replay of every recorded fixture (`opspilot investigate --mode replay --decision approve
  --approver replay-eval`, Weaviate retrieval, `qwen3:4b`, M-series 8 GB). One run each,
  final code:

  | fixture | expected | diagnosed | outcome | time | LLM calls | tokens in/out | tool calls |
  |---|---|---|---|---|---|---|---|
  | oom-payments | OOM_KILLED / payments-api | OOM_KILLED / payments-api, 0.95 | patch memory 512Mi | 142 s | 15 | 36.4k / 1.1k | 8 |
  | imagepull-orders-tag | IMAGE_PULL_ERROR / orders-api | same, 0.95 | set image to the last healthy one | 111 s | 15 | 36.8k / 0.9k | 7 |
  | missing-env-inventory | CONFIG_MISSING_ENV / inventory-api | same, 0.95 | rollback (recent rollout) | 125 s | 16 | 37.7k / 1.1k | 6 |
  | readiness-payments | READINESS_PROBE_MISCONFIG / payments-api | same, 0.95 | rollback (recent rollout) | 152 s | 16 | 33.4k / 1.1k | 8 |
  | redis-down | DEPENDENCY_UNAVAILABLE / redis | DEPENDENCY_UNAVAILABLE / payments-api | escalated: contradicts evidence | 132 s | 14 | 32.1k / 1.2k | 8 |
  | healthy (false alarm) | no fault | (model: readiness on payments-api) | escalated: no active fault found | 102 s | 15 | 35.5k / 1.1k | 4 |

  Category right in 5 of 5 faults, component right in 4 of 5; every proposal cites
  evidence; no wrong action reached approval. This is 6 runs, not an evaluation; Day 5
  scores 30 scenarios.
- Live `oom-payments`: 151 s of agent time (triage 6.8 s, retrieve 1.8 s, investigate
  85.6 s for 8 calls, diagnose 25.5 s, propose 13.3 s, execute 0.1 s, verify 10.5 s,
  report 7.8 s), 13 LLM calls, 32.0k / 0.9k tokens.
- Iterations, all measured on the oom-payments replay unless noted:
  1. Native tool calls with thinking: 4 calls in 342 s, run deadline hit, escalated.
  2. Constrained JSON steps: 73 s total, but 1 real call and 10 blocked `list_pods`
     repeats.
  3. Calls made + suggested next calls + auto-run after 2 repeats: 8 calls, describe_pod
     and logs collected, 139 s; proposal 256Mi (copied from the prompt example, below the
     service's need).
  4. Computed action defaults (4x current memory, max 512Mi) in the remediation prompt:
     512Mi proposed.
  5. Across all fixtures: `healthy` was diagnosed READINESS_PROBE_MISCONFIG at 0.95 from
     old Unhealthy events, `readiness-payments` escalated because the model put R3 in
     `evidence_refs`, and `redis-down` proposed scaling payments-api. Added the
     healthy-pods contradiction check (one repair turn, then escalate), moving E/R ids to
     the right field in code, and suggestions for services named in error logs. Healthy
     and readiness now pass; redis-down escalates instead of acting.
- Tests: 354 unit tests passed, 87% line coverage of `src/opspilot` (`make test`); every
  commit of the day passes the unit tests on its own.

**Known issues / debt:**
- redis-down: the 8-call budget runs out before the agent checks redis itself (it spends
  a call and two turns on `previous=true` logs for a pod that never restarted, and follows
  a stale payments-api error line). It escalates safely but misses the component. Budget
  and suggestion order are Day 5 experiments.
- The recorded fixtures contain older Unhealthy events and log lines from earlier fault
  runs. That is realistic noise, and it caused the healthy false positive before the
  contradiction check.
- `qwen3:4b` is Qwen3-4B-2507, which cannot turn thinking off for free-form replies; only
  schema-constrained calls are fast. Native tool calling (~85 s per step) stays as an
  option, not the default.
- Environment variable values are hidden from the agent by design, so the memory a service
  needs is usually unknown; the 4x default is a heuristic and verification reports
  `not_resolved` if it is too small.
- ReplicaSet creation times are unreliable for "recent rollout" (an old ReplicaSet is
  reused when a template is reverted), so recency comes from `ScalingReplicaSet` events.
- One replay batch hit Ollama timeouts and a Weaviate connection timeout under memory
  pressure; retrieval failures now degrade to no-RAG instead of failing the run.
- The approver name is self-asserted (single machine, no SSO).

**Next:**
- Day 5: the 30-scenario fault suite, agent eval runners and scorers (category, component,
  citation validity, action correctness, safety), RAG and suggestion ablations, safety
  tests (injection, budget, approval bypass).

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
