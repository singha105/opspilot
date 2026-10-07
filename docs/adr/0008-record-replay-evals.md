# ADR-0008: Record/replay of tool calls for deterministic evals

- Status: accepted
- Date: 2026-10-07

## Context

Agent evals must be repeatable. A live run depends on pod names, timing, restart counts
and log lines that change every time a fault is injected, and it needs the cluster
running, which costs memory and minutes on an 8 GB laptop. Changing a prompt should not
also change the incident it is tested against.

## Decision

- `opspilot faults record <id> | --all` injects a scenario through the admin context,
  waits for the expected symptom plus 20 s to settle, then calls **every** read tool
  exhaustively for the namespace: list_pods (whole namespace and per Deployment
  label), describe_pod for each pod, current and previous logs (200 lines) for each
  container, events namespace-wide and per object (120 minutes), each Deployment and its
  rollout history, services and endpoints, every ConfigMap, nodes and PVCs. Then it resets.
- Recording goes through the same `K8sTools` code path as the agent, with the reader
  identity, so fixtures contain exactly what the model would see: redacted, capped, and
  including real error answers (for example a 400 for `previous=true` on a pod that never
  restarted).
- A fixture (`evals/fixtures/<id>.json`) maps a canonical key (tool name + arguments with
  defaults filled and nulls dropped, sorted JSON) to the recorded output, plus metadata:
  Kubernetes version, recording time, scenario file SHA-256, identity, symptom time and
  call count. Fixtures are never edited by hand; re-recording replaces them.
- `opspilot-k8s --mode replay --fixture <path>` serves answers from the fixture through
  the same server code. Two derivations are allowed because they cannot invent data:
  fewer log lines or a `contains` filter from the 200 recorded lines, and a shorter event
  window from the 120-minute recording. Any other unrecorded call returns
  `{"error": {"type": "not_recorded", "hint": "Try broader calls: list_pods ..."}}`.
- Replay is read-only by construction: the actions server is never started in replay mode.

## Alternatives considered

- **Mocking the Kubernetes API in the agent.** Hand-written responses drift from reality
  and would be shaped by what the author expects the agent to need.
- **Recording only the calls a reference agent made.** Smaller fixtures, but a different
  agent (or prompt) that investigates differently would hit unrecorded calls. Recording
  everything removes that bias.
- **VCR-style HTTP cassettes.** Tied to the client library's exact requests and harder
  to read; the tool-level fixture is readable JSON a reviewer can inspect.

## Consequences

- Six fixtures (five scenarios plus a healthy baseline), 39–43 calls and 61–67 KB each.
  A unit test replays every recorded call through the server path and checks the output
  is identical; another re-records each fixture from its own replay and gets the same
  calls back.
- Agent evals (Day 4–5) run without the cluster, in seconds, with identical inputs per run.
- Fixtures age: they capture one moment, including unrelated recent events (the healthy
  baseline contains older "Unhealthy" warnings from earlier tests). That is realistic
  noise, and it is part of what the agent must ignore.
- Pod names in fixtures are real and random; the agent has to discover them with
  `list_pods`, as it would live.
