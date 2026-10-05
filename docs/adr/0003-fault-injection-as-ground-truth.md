# ADR-0003: Fault injection as ground truth

- Status: accepted
- Date: 2026-10-05

## Context

To claim the agent finds root causes, we need incidents whose root cause is known
for certain. Hand-written incident stories are easy to make accidentally easy,
and real production incidents are not available. We also need the same incident
reproducible on demand so that prompt or retrieval changes can be compared fairly.

## Decision

- Every eval case is a scenario file in `faults/scenarios/<id>.yaml`. It holds the
  injection, the expected cluster symptom, the alert payload and the ground truth:
  root-cause category, faulty component, relevant runbook ids and acceptable
  remediation actions. A Pydantic schema validates every file. The category must
  come from `opspilot.models.RootCauseCategory` and the id must match the file name.
- The demo app is driven entirely by environment variables, so most faults are
  configuration changes (env, image, probe, replica count) applied to a real
  Deployment rather than mocks.
- The injector uses only the admin context. It confirms the expected symptom
  (pod reasons such as `OOMKilled`, or Deployments with too few ready replicas)
  before an eval counts, and reset replaces the Deployment from the rendered base
  manifests and waits for the whole namespace to be ready again.
- Demo Deployments roll out with `maxSurge: 0, maxUnavailable: 1`. With a single
  replica, the default surge strategy keeps the old pod serving, so a broken
  readiness probe or bad image would never cause a visible outage. Replacing in
  place makes the injected fault show the symptom described in its alert. It also
  avoids needing memory for a second copy of each pod.
- Agent code must never special-case scenario ids. Scenarios are split into `dev`
  and `test` (finalised on Day 5) so tuning cannot target the test set.

## Alternatives considered

- **Static recorded incidents only.** Cheap to run, but they cannot show that the
  agent works against a live cluster. Recorded fixtures will still back fast,
  repeatable evals (Day 3 record/replay); live injection is what produces them.
- **Chaos tooling (Chaos Mesh, Litmus).** Powerful, but too heavy for 8 GB, and
  most of our faults are misconfigurations rather than infrastructure chaos.
- **`kubectl apply` for reset.** A three-way merge leaves fields added by the
  injection in place (for example an extra env var), so reset uses `replace`.

## Consequences

- Day 1 ships 5 scenarios, covering OOM, image pull, missing env, readiness probe
  and dependency outage. Each injects, shows its symptom and resets cleanly in
  the integration tests. Symptom time p50 is 6.9 s and reset p50 is 12.2 s.
- Some categories (scheduling, PVC, init containers) will need injection types
  beyond env/image/probe/scale. The schema's discriminated union makes those easy
  to add on Day 5.
