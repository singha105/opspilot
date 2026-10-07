# ADR-0011: Strategies for running the agent on a 4B local model

- Status: accepted
- Date: 2026-10-07

## Context

The agent must run on an 8 GB Mac with `qwen3:4b` through Ollama (ADR-0001). The model
pulled under that tag is Qwen3-4B-2507, a variant that always thinks: its chat template
opens a `<think>` block, and thinking cannot be switched off for free-form replies. With a
JSON-schema `format`, Ollama's grammar forces JSON from the first token, which skips the
thinking; native tool calls cannot use that. The first end-to-end
replay of `oom-payments` used native tool calling with thinking on, and the numbers ruled
that design out:

| design | investigation | whole run | outcome |
|---|---|---|---|
| native tool calls, thinking on | 4 calls in 342 s (~85 s per call) | 360 s, run deadline hit | escalated: diagnosis timed out |
| constrained JSON step, thinking off | 1 call in 33 s, then it stopped | 73 s | OOM_KILLED, cited, but thin evidence |
| JSON step + calls made + suggestions | 8 calls in 83-86 s | 91-151 s | OOM_KILLED, cited, pod details and logs |

Other problems seen in the same runs: the JSON-step model repeated `list_pods` ten times
in a row once it had a result; and with free choice of a memory limit it copied the
256Mi from the prompt's example, which is below what the demo service allocates (the
agent cannot see environment variable values, by design).

## Decision

1. **Thinking only where it pays.** Structured roles (triage, tool choice, diagnosis,
   remediation, report) run with thinking off and Ollama's JSON-schema output; the native
   tool-calling path stays available as `OPSPILOT_AGENT_TOOL_STRATEGY=native`.
2. **One investigation step = one JSON object** `{"tool", "args", "reason"}` whose `tool` is
   an enum of the real tool names (plus `finish_investigation`). Arguments are validated by
   the MCP servers, and errors come back as evidence.
3. **Prompt scaffolding computed by code:** the calls already made, and up to four
   suggested next calls derived from the evidence with generic SRE rules (list pods first;
   check the endpoints of services that error logs name as `host:port`; describe and read
   logs of unhealthy pods, the affected service's pods and pods that restarted first; then
   the Deployment, its rollout history and events). The model may
   ignore them. After two blocked repeats in a row the top suggestion runs automatically,
   which is logged as `tool_auto`.
4. **Defaults for action parameters computed by code** and shown next to each allowed
   action. For memory: 4x the current limit, at least 256Mi, at most 512Mi (the actions
   server's bound). When the evidence does not show how much memory a process needs, one
   generous step that a human approves beats a second incident cycle.
5. **Deterministic evidence summaries** per tool, short excerpts, and `E`/`R` ids, instead
   of raw tool JSON in the prompt.
6. **Repair, then fall back:** validation errors are quoted back (at most two repair turns),
   then JSON is extracted from the raw text, then a non-model fallback is used.
7. **Per-role output limits** (256 tokens for a tool choice up to 1,536 for native tool
   calls), temperature 0, seed 42, `num_ctx` 8192, one model instance per role per run.

## Alternatives considered

- **Native tool calling with thinking.** Most "agentic", but about 85 s per step here; an
  8-call investigation would take longer than the whole run budget.
- **A smaller or non-thinking model** (e.g. a 1.7B model, or the older Qwen3-4B with a
  working `/no_think`). Faster, but tool selection and diagnosis quality would need a new
  evaluation; kept as a Day 5 experiment behind `OPSPILOT_LLM_MODEL`.
- **A fixed investigation script with no model choice.** Fast and predictable, but cannot
  follow evidence that points somewhere unexpected (a dependency, an event about another
  object). Suggestions keep that predictability while leaving the choice to the model.
- **Larger memory steps or scenario-specific rules.** Ruled out: no scenario ids or
  expected answers in agent code or prompts (a test checks the prompts).

## Consequences

- Replays of the five Day 4 scenarios take 102-152 s, and the live `oom-payments` run took
  151 s of agent time, instead of hitting the 360 s deadline.
- The suggestions encode SRE habits, so the evidence is more complete, but they also steer
  the model; Day 5 measures accuracy with and without them, and with and without RAG.
- The 4x memory default fits services that are a few times over their limit; a service
  that needs more than 512Mi still needs a manual change, and verification will report
  `not_resolved` if the step is too small.
- Prompts are versioned (the investigation prompt is `investigate-v2` after this change)
  and each run logs which version it used, so eval results can be tied to the prompt that
  produced them.
