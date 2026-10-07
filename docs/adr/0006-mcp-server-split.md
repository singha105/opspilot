# ADR-0006: Three MCP servers split by privilege

- Status: accepted
- Date: 2026-10-07

## Context

The agent needs to read the cluster, search the knowledge base and, rarely, change a
Deployment. An LLM decides which tool to call, and its inputs include text an attacker
may control (logs, events, documents). If reading and writing live in one tool server,
one confused call can turn an investigation into a change. Tool access should be
something we can reason about per process, not per prompt.

## Decision

Expose tools through three stdio MCP servers (FastMCP, `mcp` 1.30), each with one job
and one identity:

| server | tools | identity | can change the cluster |
|---|---|---|---|
| `opspilot-k8s` | 11 read tools | `opspilot-reader` kubeconfig | no (RBAC and code) |
| `opspilot-kb` | `search_knowledge`, `get_document` | none (local index) | no |
| `opspilot-actions` | `plan_action`, `execute_action` | operator for writes, reader for reads | only with an approval token |

- The agent loads `k8s` and `kb` by default. `actions` is added only when explicitly
  requested (`opspilot.tools.load_tools(include_actions=True)`) and never in replay.
- The k8s server has no write tool at all, so a write is impossible even if RBAC were
  misconfigured. The reader ServiceAccount cannot write either: two independent layers.
- All three servers share one execution path (`mcp_servers/common`): namespace
  allowlist, redaction, a 6,000-character output cap with a truncation hint, clean
  error objects and an audit record per call.
- Tool descriptions are written as prompts: when to use the tool, what it returns,
  its limits and one example call, in under 80 words.

## Alternatives considered

- **One server with a `read_only` flag.** Less code, but the flag becomes the security
  boundary, and the model sees write tools in its catalog during every investigation.
- **Calling the Kubernetes API directly from LangChain tools.** Simple, but loses the
  standard protocol: no Inspector, no reuse from other MCP clients, and no process
  boundary between the model and the credentials.
- **HTTP transport.** Useful for a shared deployment, but stdio keeps credentials local
  to a child process and needs no network listener on a laptop.

## Consequences

- Any MCP client (the Inspector, IDE assistants, desktop chat apps) can use the read
  servers as-is; `docs/mcp.md` has the client configuration.
- Three processes per agent run. Each starts in about a second; the knowledge server
  loads its models lazily on the first search.
- FastMCP validates argument types before our code runs. Bounds (`tail_lines`,
  `since_minutes`, `k`) are therefore advertised in the schema but enforced inside the
  audited path, so out-of-range calls are logged as rejections instead of disappearing.
