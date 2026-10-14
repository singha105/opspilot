# Architecture

OpsPilot is a Python package (`src/opspilot`) with three entry points over one service
layer, a LangGraph agent, three MCP servers, and local infrastructure started by `make`.

## Components

```mermaid
flowchart LR
  subgraph Clients
    W[Alertmanager webhook] --> API
    U[Operator console<br/>Streamlit] --> S
    CLI[opspilot CLI] --> S
    API[HTTP API<br/>FastAPI] --> S
  end
  S[IncidentService<br/>service.py] --> G[LangGraph agent<br/>agent/graph.py]
  S --> CP[(SQLite checkpoints<br/>runs/ state + reports)]
  G -->|structured calls| L[(Ollama<br/>qwen3:4b)]
  G -->|retrieve, refine| R[(Weaviate hybrid<br/>Chroma, Pinecone)]
  G -->|11 read-only tools| K[MCP: opspilot-k8s]
  G -->|search_knowledge| B[MCP: opspilot-kb] --> R
  G -->|plan + execute with token| X[MCP: opspilot-actions]
  K -->|reader SA: get/list/watch| C[(k3d cluster<br/>namespace shop)]
  X -->|operator SA: narrow writes| C
  K -.->|replay mode| F[(Recorded fixtures<br/>evals/fixtures)]
  H[Human approver] -->|approve / reject / edit| S
```

| Component | Code | Role |
|---|---|---|
| HTTP API | `api/app.py` | `POST /alerts` (Alertmanager webhook or simple JSON), incident reads, `POST /incidents/{id}/approval`, `/healthz`; bearer token on writes |
| Operator console | `ui/app.py`, `ui/views.py` | Incidents, Runs, Evals and Knowledge pages |
| CLI | `cli.py`, `agent/cli.py` | `investigate`, `resume`, `runs`, `kb`, `faults`, `eval` |
| Service layer | `service.py` | create, run up to approval, resume from a decision, list and read incidents; one agent run at a time |
| Agent | `agent/` | 11-node state machine ([agent.md](agent.md), ADR-0009) |
| Prompts | `prompts/*.md` | versioned, 5-part prompts |
| Retrieval | `rag/` | chunking, fastembed embeddings, Weaviate/Chroma/Pinecone stores, RRF, FlashRank ([rag.md](rag.md)) |
| MCP servers | `mcp_servers/` | `opspilot-k8s` (read-only, live or replay), `opspilot-kb`, `opspilot-actions` (gated) ([mcp.md](mcp.md)) |
| Approval tokens | `agent/approval.py` | HMAC tokens bound to an action hash, single use, 10 minutes |
| Faults and evals | `faults/`, `evals/` | scenario injection and recording; scoring, replay runner, live subset, report |
| Infrastructure | `infra/`, `demo/` | Weaviate (and optional Phoenix, app containers) in compose; k3d cluster; Shopfront demo app; RBAC |

## From alert to resolution

```mermaid
sequenceDiagram
  autonumber
  participant AM as Alertmanager
  participant API as HTTP API
  participant SVC as IncidentService
  participant AG as Agent (LangGraph)
  participant KB as Knowledge base
  participant K8S as opspilot-k8s (read-only)
  participant LLM as qwen3:4b
  participant H as Human
  participant ACT as opspilot-actions

  AM->>API: POST /alerts (bearer token)
  API->>SVC: create incident, run in background
  SVC->>AG: start(alert)
  AG->>LLM: triage
  AG->>KB: hybrid search + rerank (6 chunks)
  loop at most 8 tool calls
    AG->>LLM: next read-only call (constrained JSON)
    AG->>K8S: list_pods / describe_pod / logs / events / ...
    K8S-->>AG: redacted, size-capped output -> evidence E#
  end
  AG->>KB: search with the evidence's failure signals (+3 chunks)
  AG->>LLM: diagnosis citing E# and R#
  AG->>AG: guards: citations, contradiction, confidence, allowlist
  AG->>ACT: plan_action (dry run, diff, risks)
  AG-->>SVC: interrupt(): approval request, checkpointed
  SVC-->>H: proposal in the console / GET /incidents/{id}
  H->>SVC: approve (console, API or CLI, any process)
  SVC->>AG: resume(decision)
  AG->>AG: mint single-use token for this action hash
  AG->>ACT: execute_action(action, token)
  ACT->>ACT: verify token, bounds, apply with operator identity
  AG->>K8S: verify: Deployment ready again?
  AG->>SVC: report.md, state.json
```

## Where things run

| Piece | `make demo` (replay) | `make demo-live` | `make app-up` |
|---|---|---|---|
| API, console | host processes (uv) | host processes | containers (compose profile `app`) |
| Ollama | host | host | host, via `host.docker.internal` |
| Weaviate | container | container | container |
| Kubernetes | none: recorded fixtures | k3d cluster | none: recorded fixtures |
| Writes | none: approved actions are simulated | operator identity via the actions server | none |

State is on disk: `runs/<incident>/` holds `run.json`, `events.jsonl`, `state.json`,
`pending.json` while a decision is due, and `report.md`; `data/checkpoints.sqlite` holds the
LangGraph checkpoints. Because every client goes through `IncidentService` and the state is
on disk, an incident started by the API can be approved in the console or with
`opspilot resume`.

## Design decisions

See the [ADR index](adr/README.md).
