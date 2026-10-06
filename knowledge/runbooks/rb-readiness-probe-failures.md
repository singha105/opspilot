---
id: rb-readiness-probe-failures
title: Pods Running but never Ready (readiness probe failing)
doc_type: runbook
categories: [READINESS_PROBE_MISCONFIG]
services: [payments-api, orders-api, inventory-api]
tags: [probes, readiness, endpoints, not-ready]
severity: high
last_reviewed: 2026-08-02
related: [rb-liveness-probe-failures, rb-dependency-unavailable, rb-service-misconfig, pm-2026-04-payments-readiness-path, k8s-probes]
---
# Pods Running but never Ready (readiness probe failing)

## Symptoms
- `kubectl get pods` shows `0/1` READY with status `Running` and no restarts.
- The Service has no endpoints, so callers get `Connection refused` immediately.
- Deployment shows `0` available replicas; alert `KubeDeploymentReplicasMismatch`.
- Events: `Readiness probe failed: HTTP probe failed with statuscode: 404` or `503`.

## Quick checks (read-only)
```bash
kubectl -n shop get pods
kubectl -n shop get endpointslices -l kubernetes.io/service-name=<service>
kubectl -n shop describe pod <pod> | grep -A6 Readiness
kubectl -n shop get events --field-selector reason=Unhealthy --sort-by=.lastTimestamp
kubectl -n shop get deploy <deployment> -o jsonpath='{.spec.template.spec.containers[0].readinessProbe}'
kubectl -n shop logs <pod> --tail=30
```
Shopfront readiness is `GET /readyz` on port 8080, period 5s, failure threshold 2.
`/readyz` returns 503 while any configured dependency is unreachable, so a NotReady
pod can be correct behaviour.

## Diagnosis decision tree
1. What status code does the probe get?
   - `404`: the probe path does not exist on the service. This is a probe
     misconfiguration; the application is fine. Compare the path with `/readyz`.
   - `503`: the service is reporting a real problem, usually an unreachable dependency.
     Read the logs for `call to <dep> failed` and switch to the dependency runbook.
   - `connection refused` or timeout: wrong port, or the process is busy.
2. Did the probe definition change in the latest rollout? `rollout history` plus the
   timing of the first `Unhealthy` event usually settles it.
3. Is only one service NotReady, or several? With Shopfront's dependency chain,
   `payments-api` NotReady makes `orders-api` NotReady too (orders calls payments).
   Fix the deepest service first.

## Remediation options
- Probe path or port wrong: patch the probe back (`patch_probe`) or roll back
  (`rollback_deployment`).
- Real dependency failure: fix the dependency; do not touch the probe.

## Do NOT
- Do not remove the readiness probe to "get endpoints back"; traffic would reach pods
  that cannot serve.
- Do not restart pods; a NotReady pod with a bad probe stays NotReady after restart.

## Verify recovery
- `kubectl -n shop get endpointslices` lists the pod IP again.
- The pod is `1/1` Ready and dependents (orders-api) become Ready within a minute.

## Escalation
Owning team of the service whose probe changed.

## Related
- [rb-liveness-probe-failures](rb-liveness-probe-failures.md)
- [rb-dependency-unavailable](rb-dependency-unavailable.md)
- [rb-service-misconfig](rb-service-misconfig.md)
- [pm-2026-04-payments-readiness-path](../postmortems/pm-2026-04-payments-readiness-path.md)
- [k8s-probes](../k8s-docs/k8s-probes.md)
