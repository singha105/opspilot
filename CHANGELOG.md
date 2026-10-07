# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-10-07

### Added
- LangGraph agent (`opspilot.agent`): ingest, triage, retrieve, investigate, diagnose,
  propose, human approval, execute, verify and report nodes over a typed
  `IncidentState`, checkpointed in SQLite.
- Human approval with `interrupt()`: approve, reject or edit (parameters only), from the
  same process or later with `opspilot resume`; approval tokens are minted only after a
  decision.
- Guards: citation checks, prompt-injection detection, a category-to-action allowlist,
  escalation on `UNKNOWN`, low confidence or a diagnosis the pod evidence contradicts,
  and per-run budgets.
- Ollama model factory with per-role settings, structured output with repair turns, and
  versioned prompts with a check against scenario-specific content.
- Constrained-JSON tool selection with suggested next calls for small models; native
  tool calling remains available (`OPSPILOT_AGENT_TOOL_STRATEGY`).
- `opspilot investigate | resume | runs`, per-run event logs and state in `runs/`, and
  optional Phoenix tracing (`tracing` dependency group).
- `docs/agent.md`, ADR-0009, ADR-0010 and ADR-0011.

## [0.3.0] - 2026-10-07

### Added
- `opspilot-k8s`: read-only Kubernetes MCP server with 11 tools, live and replay modes.
- `opspilot-kb`: knowledge-base MCP server (`search_knowledge`, `get_document`).
- `opspilot-actions`: gated remediation server with dry-run planning and execution
  behind single-use HMAC approval tokens (`opspilot.agent.approval`).
- Shared MCP layer: namespace allowlist, redaction, 6,000-character output cap,
  structured errors, 10 s timeouts and a JSONL audit log.
- `opspilot faults record` and six recorded fixtures (five scenarios plus healthy).
- `opspilot.tools`: LangChain tools over the MCP servers; `scripts/mcp_smoke.py`.
- `docs/mcp.md`, `docs/security.md`, ADR-0006, ADR-0007 and ADR-0008.

### Fixed
- The package version now matches the release (0.2.0 shipped with version 0.1.0).

## [0.2.0] - 2026-10-05

### Added
- Knowledge base: 22 runbooks, 10 postmortems, 4 service cards and 16 Kubernetes
  documentation pages (CC BY 4.0) with a validator that runs in CI.
- RAG pipeline: Markdown-section and fixed chunkers, cached bge-small embeddings,
  Weaviate, Chroma and optional Pinecone stores behind one protocol, weighted RRF,
  FlashRank reranking and a citing retriever.
- `opspilot kb ingest | search | stats` and `opspilot eval check-queries | retrieval`.
- 60-query retrieval evaluation with a 28-configuration results matrix, a chart and
  a CI smoke check.
- ADR-0004 (retrieval defaults) and ADR-0005 (vector store abstraction), `docs/rag.md`.

### Changed
- Fault scenarios list their ground-truth runbooks.
- Weaviate's gRPC port is published on 50052.

## [0.1.0] - 2026-10-05

### Added
- uv-managed Python 3.12 package `opspilot` with settings, JSON logging and the
  `opspilot` CLI.
- Makefile with setup, quality, infrastructure, demo, fault and housekeeping targets.
- Docker Compose stack with Weaviate (all modules disabled) and an opt-in Phoenix
  tracing profile.
- k3d cluster configuration for a lean single-node cluster.
- Shopfront demo app: one env-driven service image plus redis, deployed with kustomize.
- RBAC for the read-only `opspilot-reader` and the narrowly scoped
  `opspilot-operator`, with a script that writes short-lived kubeconfigs.
- Fault-injection framework: scenario schema, injector, CLI and five scenarios
  with ground-truth root causes.
- Unit and integration tests, pre-commit hooks and GitHub Actions CI.
- ADRs 0001–0003, README and progress log.

[Unreleased]: https://github.com/singha105/opspilot/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/singha105/opspilot/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/singha105/opspilot/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/singha105/opspilot/releases/tag/v0.1.0
