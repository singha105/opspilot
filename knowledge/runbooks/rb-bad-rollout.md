---
id: rb-bad-rollout
title: Regression introduced by the latest Deployment rollout
doc_type: runbook
categories: [BAD_ROLLOUT]
services: [payments-api, orders-api, inventory-api]
tags: [rollout, rollback, release, regression]
severity: high
last_reviewed: 2026-08-05
related: [rb-image-pull-error, rb-oom-killed, rb-readiness-probe-failures, k8s-deployment]
---
# Regression introduced by the latest Deployment rollout

## Symptoms
- Errors, restarts or NotReady pods start within minutes of a new ReplicaSet.
- `kubectl rollout status` hangs with `Waiting for deployment ... rollout to finish`,
  or reports `ProgressDeadlineExceeded`.
- Error rate or latency in callers jumps at the deploy time, while nothing changed in
  dependencies.

## Quick checks (read-only)
```bash
kubectl -n shop rollout status deploy/<deployment> --timeout=5s
kubectl -n shop rollout history deploy/<deployment>
kubectl -n shop rollout history deploy/<deployment> --revision=<N>
kubectl -n shop get rs -l app.kubernetes.io/name=<deployment>
kubectl -n shop get deploy <deployment> -o jsonpath='{.status.conditions}'
kubectl -n shop get events --sort-by=.lastTimestamp | tail -20
```
Shopfront keeps the last 3 ReplicaSets (`revisionHistoryLimit: 3`). Because rollouts
use `maxSurge: 0`, the old pod is gone before the new one is Ready.

## Diagnosis decision tree
1. Does the start of the incident line up with the newest ReplicaSet's creation time?
   If not, a rollout is unlikely to be the cause.
2. What changed between revisions? Diff the pod templates of the current and previous
   revision: image, env, probes, resources, command.
3. Does the failure match a more specific runbook (image pull, OOM, missing env,
   probes, bad command)? Prefer the specific diagnosis when the evidence is clear, and
   use this runbook when the regression is in the application behaviour itself
   (for example 5xx responses from new code).
4. Is the old revision known to be good? Check that it ran without restarts.

## Remediation options
- Roll back to the previous revision (`rollback_deployment`). This is the default
  first move for an obvious deploy regression.
- If rollback is not possible (schema migration), escalate to the owning team.

## Do NOT
- Do not roll forward with a hot fix during the incident without the owning team.
- Do not delete ReplicaSets; they are the rollback history.

## Verify recovery
- `rollout status` succeeds and the pod template matches the previous good revision.
- Error rate and restarts return to baseline.

## Escalation
Owning team; include the revision numbers and the template diff in the page.

## Related
- [rb-image-pull-error](rb-image-pull-error.md)
- [rb-oom-killed](rb-oom-killed.md)
- [rb-readiness-probe-failures](rb-readiness-probe-failures.md)
- [k8s-deployment](../k8s-docs/k8s-deployment.md)
