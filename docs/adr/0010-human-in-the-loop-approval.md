# ADR-0010: Human-in-the-loop approval with interrupt() and checkpoints

- Status: accepted
- Date: 2026-10-07

## Context

OpsPilot may propose a change to a running system. The decision to make it belongs to a
person, who may not answer for minutes or hours, and who may answer from a different
terminal than the one that started the run. Approval must also be impossible to fake from
inside the run: the model, a prompt injection or a bug must not be able to "approve" on a
human's behalf.

## Decision

- `human_approval` calls LangGraph's `interrupt()` with the proposal (diagnosis, action,
  dry-run diff, risk notes, rollback plan, security flags). The run stops there; its state
  is checkpointed in SQLite (`data/checkpoints.sqlite`) with the incident id as thread id.
- A decision is an `ApprovalDecision` (`approve | reject | edit`, approver, reason, and for
  `edit` only parameter changes). It is passed back with `Command(resume=decision)`, from
  the same process or from `opspilot resume <incident>` in another one.
- An edit may change parameters but not the action type, and the edited action is
  validated again against the allowlist and server bounds; anything invalid becomes a
  rejection.
- The approval token (ADR-0007) is minted in `execute`, after the decision, from the
  approver named in it. Nothing before `interrupt()` has side effects, because LangGraph
  re-runs a node from the top when it resumes.
- Live runs refuse `--decision approve` given up front: a change can only be approved after
  the proposal has been shown. Replay runs never execute anything (the action result is
  marked simulated), so a scripted decision is allowed there for evaluations.
- Waiting for a human is not agent time: the resumed run's budget starts at the decision.
- Checkpointed state types are listed explicitly for deserialization; a test fails if a
  new state type is not allowlisted, because unlisted types are restored as plain dicts.

## Alternatives considered

- **Synchronous prompt inside the process.** Simplest, but the decision is lost if the
  process dies, and it cannot come from another terminal or a future UI.
- **Approval via a field the agent sets.** Any model or injection could set it.
- **External workflow engine.** More robust for production, but much heavier than this
  project needs; the LangGraph checkpointer already gives durable pauses.

## Consequences

- Tests prove: no execution without an approve/edit decision; replay never executes; a
  rejection reaches the report without execution; a run paused in one process resumes in
  another with a fresh graph and checkpointer; and bad edits become rejections.
- The checkpoint database holds incident data and is gitignored under `data/`.
- The approver's name is self-asserted on a single machine (see `docs/security.md`).
