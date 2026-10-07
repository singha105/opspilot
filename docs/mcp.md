# MCP servers

OpsPilot's tools are three [Model Context Protocol](https://modelcontextprotocol.io)
servers that speak stdio. The agent uses them through `langchain-mcp-adapters`, and any
other MCP client can use them too. Why there are three: [ADR-0006](adr/0006-mcp-server-split.md).

| server | command | identity | purpose |
|---|---|---|---|
| `opspilot-k8s` | `uv run opspilot-k8s [--mode live\|replay] [--fixture F]` | reader | read-only cluster investigation |
| `opspilot-kb` | `uv run opspilot-kb [--store weaviate\|chroma]` | none | knowledge-base search |
| `opspilot-actions` | `uv run opspilot-actions` | operator (writes), reader (reads) | gated remediation |

## Shared behaviour

Every tool call goes through `mcp_servers/common/runner.py`:

- **Namespace allowlist:** only namespaces in `OPSPILOT_ALLOWED_NAMESPACES` (default
  `["shop"]`).
- **Redaction** of bearer tokens, credentials in URLs/DSNs, AWS keys, JWTs, private keys
  and `secret=` / `token=` / `password=` style values, in both outputs and audit records.
- **Output shaping:** compact JSON, at most 6,000 characters. A result that would be
  longer is shortened (long strings clipped, then list items dropped from the end of the
  largest list) and carries `"truncated": true` with a hint on how to narrow the call.
- **Errors** are objects, never stack traces: `{"error": {"type", "message", "hint"}}`,
  with types such as `forbidden`, `not_found`, `timeout`, `invalid_argument`,
  `namespace_not_allowed`, `not_recorded`, `approval_required`.
- **Timeouts:** 10 s per Kubernetes API request.
- **Audit:** one JSON line per call in `runs/audit.jsonl` (gitignored):

```json
{"ts":"2026-10-07T06:16:04Z","server":"opspilot-k8s","tool":"get_pod_logs","args":{"namespace":"shop","name":"x","container":null,"previous":false,"tail_lines":500,"contains":null},"duration_ms":0.1,"bytes":96,"status":"rejected","mode":"live","run_id":"run-20261007-061604-e82931","error_type":"invalid_argument"}
```

All servers started for one agent run share `OPSPILOT_RUN_ID`, so a run can be traced
across servers.

## `opspilot-k8s` tool catalog

| tool | arguments | returns |
|---|---|---|
| `list_pods` | namespace, label_selector? | name, phase, ready, restarts, current reason, last termination reason, age, node |
| `describe_pod` | namespace, name | per container: image, state, lastState (reason, exitCode), restartCount, resources, probes, env var **names only**, volume mounts; volumes, conditions, recent events |
| `get_pod_logs` | namespace, name, container?, previous=false, tail_lines≤200, contains? | redacted log lines (each clipped to 400 characters) |
| `get_events` | namespace, involved_object_name?, since_minutes≤120 | events newest first: type, reason, object, count, message, age |
| `get_deployment` | namespace, name | replicas (desired/ready/updated/available), conditions, revision, strategy, selector, pod template summary |
| `get_rollout_history` | namespace, name | ReplicaSet revisions with images, change-cause, replicas, created |
| `list_services` | namespace | type, cluster IP, selector, ports |
| `get_service_endpoints` | namespace, name | ready / not-ready counts and addresses with pod names, ports |
| `get_configmap` | namespace, name | keys and values (values truncated to 500 characters). ConfigMaps only, never Secrets |
| `list_nodes` | none | kubelet version, capacity, allocatable, taints, labels, conditions |
| `list_pvcs` | namespace | status, storage class, access modes, requested/bound size, volume, events |

There is no write tool. The reader ServiceAccount cannot write or read Secrets either.

**Replay mode.** `--mode replay --fixture evals/fixtures/<id>.json` answers from a
recorded fixture (see [ADR-0008](adr/0008-record-replay-evals.md)):

```bash
uv run opspilot faults record oom-payments     # or --all: 5 scenarios + healthy baseline
uv run opspilot-k8s --mode replay --fixture evals/fixtures/oom-payments.json
```

## `opspilot-kb`

| tool | arguments | returns |
|---|---|---|
| `search_knowledge` | query, k≤10, doc_types?, categories? | cited chunks: citation_id, doc_id, doc_type, title, section, text, score |
| `get_document` | doc_id, section? | one document or one section, plus the list of its sections |

It uses the Day 2 retriever with the ADR-0004 defaults (Weaviate hybrid, α = 0.5, no
rerank). `--store chroma` works without Weaviate.

## `opspilot-actions`

| tool | arguments | behaviour |
|---|---|---|
| `plan_action` | action | validates, runs the patch with `dryRun=All`, returns diff, risk notes and `action_hash`. Changes nothing. |
| `execute_action` | action, approval_token | runs only with a valid, unexpired, single-use token for exactly this action |

Allowed actions (all in an allowlisted namespace):

```json
{"type": "restart_deployment", "namespace": "shop", "deployment": "orders-api"}
{"type": "rollback_deployment", "namespace": "shop", "deployment": "orders-api", "to_revision": 3}
{"type": "scale_deployment", "namespace": "shop", "deployment": "redis", "replicas": 1}
{"type": "patch_container_resources", "namespace": "shop", "deployment": "payments-api", "container": "app", "memory_limit": "256Mi", "cpu_limit": "500m"}
{"type": "set_container_image", "namespace": "shop", "deployment": "orders-api", "container": "app", "image": "opspilot-demo-svc:dev"}
```

Bounds: replicas 0–5, memory ≤ 512Mi, cpu ≤ 1 core, images only from the Deployment's
rollout history. Tokens: [ADR-0007](adr/0007-approval-tokens.md).

## Connecting a client

### From Python (LangChain)

```python
from opspilot.tools import load_tools, tool_text

tools = await load_tools()  # k8s + kb, live
tools = await load_tools(mode="replay", fixture=Path("evals/fixtures/oom-payments.json"))
tools = await load_tools(include_actions=True)  # adds plan/execute (live only)
pods = tool_text(
    await next(t for t in tools if t.name == "list_pods").ainvoke({"namespace": "shop"})
)
```

`scripts/mcp_smoke.py` does this end to end in either mode.

### Any MCP client (stdio)

Most desktop and IDE MCP clients accept a configuration like this (adjust the path):

```json
{
  "mcpServers": {
    "opspilot-k8s": {
      "command": "uv",
      "args": ["--directory", "/path/to/opspilot", "run", "opspilot-k8s"]
    },
    "opspilot-kb": {
      "command": "uv",
      "args": ["--directory", "/path/to/opspilot", "run", "opspilot-kb", "--store", "chroma"]
    },
    "opspilot-k8s-replay": {
      "command": "uv",
      "args": ["--directory", "/path/to/opspilot", "run", "opspilot-k8s",
               "--mode", "replay", "--fixture", "evals/fixtures/oom-payments.json"]
    }
  }
}
```

### MCP Inspector

```bash
npx @modelcontextprotocol/inspector uv run opspilot-k8s                      # web UI
npx @modelcontextprotocol/inspector --cli uv run opspilot-k8s --method tools/list
npx @modelcontextprotocol/inspector --cli uv run opspilot-k8s \
  --method tools/call --tool-name get_service_endpoints \
  --tool-arg namespace=shop --tool-arg name=redis
```

Both read servers were verified this way with Inspector 2.9.0.

## Prerequisites

- `make cluster-up demo-deploy rbac-apply` for live mode (kubeconfigs in `.secrets/`,
  valid for 12 hours; refresh with `make kubeconfigs`).
- `make infra-up` and `uv run opspilot kb ingest` for `opspilot-kb` with Weaviate.
- `OPSPILOT_APPROVAL_SECRET` in `.secrets/.env` for `opspilot-actions`.
