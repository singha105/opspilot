# Architecture decision records

Each record has Context, Decision, Alternatives considered and Consequences. Records are
not edited after the fact; a later record that changes a decision says so.

| ADR | Decision | Status | Date |
|---|---|---|---|
| [0001](0001-local-first-zero-cost.md) | Local-first, zero-cost stack | accepted | 2026-10-05 |
| [0002](0002-read-only-by-default-agent-access.md) | Read-only by default agent access | accepted | 2026-10-05 |
| [0003](0003-fault-injection-as-ground-truth.md) | Fault injection as ground truth | accepted | 2026-10-05 |
| [0004](0004-retrieval-defaults.md) | Retrieval defaults | accepted; reranking for the agent changed by 0012 | 2026-10-05 |
| [0005](0005-vector-store-abstraction.md) | Vector store abstraction | accepted | 2026-10-05 |
| [0006](0006-mcp-server-split.md) | Three MCP servers split by privilege | accepted | 2026-10-07 |
| [0007](0007-approval-tokens.md) | Single-use approval tokens for write actions | accepted | 2026-10-07 |
| [0008](0008-record-replay-evals.md) | Record/replay of tool calls for deterministic evals | accepted | 2026-10-07 |
| [0009](0009-state-machine-over-free-form-react.md) | A fixed state machine instead of a free-form ReAct agent | accepted | 2026-10-07 |
| [0010](0010-human-in-the-loop-approval.md) | Human-in-the-loop approval with interrupt() and checkpoints | accepted | 2026-10-07 |
| [0011](0011-small-model-strategies.md) | Strategies for running the agent on a 4B local model | accepted | 2026-10-07 |
| [0012](0012-evaluation-methodology.md) | Evaluation methodology for the agent | accepted | 2026-10-08 |

By topic: safety and access (0002, 0006, 0007, 0010), retrieval (0004, 0005), the agent
(0009, 0011), evaluation (0003, 0008, 0012), and the overall stack (0001).
