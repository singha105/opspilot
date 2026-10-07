# ADR-0009: A fixed state machine instead of a free-form ReAct agent

- Status: accepted
- Date: 2026-10-07

## Context

The agent runs on a 4B local model. Free-form ReAct ("think, pick any tool, repeat until
you decide to answer") leaves every decision to the model: when to search, when to stop
investigating, when to diagnose, whether to change something. Small models drift, loop
and skip steps, and an incident tool must never reach a write action because the model
lost track of where it was. Reviewers also need to see, and test, every path a run can
take.

## Decision

OpsPilot is a LangGraph `StateGraph` with ten nodes and three conditional edges:

```
ingest_alert -> triage -> retrieve -> investigate -> diagnose
diagnose            -> report (escalated)        | propose_remediation
propose_remediation -> human_approval (action)    | report (manual change / none)
human_approval      -> execute (approve or edit)  | report (reject)
execute -> verify -> report -> END
```

- The model is used where judgement helps: triage, choosing the next read call, diagnosis,
  choosing remediation parameters, the follow-up section of the report. Everything else
  (retrieval, evidence summaries, routing, guards, execution, verification, the report
  layout) is deterministic code.
- The only open-ended loop is `investigate`, and it is bounded: at most 8 tool calls,
  repeated calls blocked, one call per turn, and a run deadline.
- State is a typed Pydantic model (`IncidentState`). Evidence and security flags are
  append-only, so no node can erase what an earlier one found.
- Routing is in code (`after_diagnose`, `after_propose`, `after_approval`), so the
  conditions for reaching `execute` are a few lines that tests cover directly.

## Alternatives considered

- **Prebuilt ReAct agent (`create_react_agent`).** Least code, but the model controls the
  control flow, which is exactly what a 4B model does worst; tool budgets and approval
  gates become prompt suggestions.
- **A plain Python pipeline without LangGraph.** Simple, but loses checkpointing and
  `interrupt()`, which the human-approval pause relies on (ADR-0010).
- **Planner-executor with a model-written plan.** More flexible, but the plan itself is
  another output to validate, with little benefit for a well-understood workflow.

## Consequences

- Every path is tested at the graph level (`tests/unit/test_agent_graph.py`): escalation,
  citation repair, rejection, prompt injection, the tool budget, and resuming after a
  restart.
- The agent cannot skip diagnosis or approval, and cannot loop forever.
- New capabilities mean new nodes or edges, not prompt changes, which keeps behaviour
  reviewable.
- Less flexible than a free-form agent: an incident that needs an unusual sequence of tools
  is limited to the 8 calls of `investigate`. That is acceptable for the target categories.
