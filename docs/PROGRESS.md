# Progress

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
