---
id: rb-liveness-probe-failures
title: Healthy containers restarted by a failing liveness probe
doc_type: runbook
categories: [LIVENESS_PROBE_MISCONFIG]
services: [payments-api, orders-api, inventory-api, redis]
tags: [probes, liveness, restarts, healthz]
severity: medium
last_reviewed: 2026-07-30
related: [rb-readiness-probe-failures, rb-oom-killed, pm-2026-05-orders-liveness-timeout, k8s-probes]
---
# Healthy containers restarted by a failing liveness probe

## Symptoms
- Restart count climbs steadily (every 30–60 seconds) even though the application logs
  look normal right up to the restart.
- Events show `Liveness probe failed: HTTP probe failed with statuscode: 404`,
  `... context deadline exceeded` or `connection refused`, followed by
  `Container app failed liveness probe, will be restarted`.
- The last termination reason is `Error` or `Completed` with exit code 137 or 143 caused by
  the kubelet's kill, not `OOMKilled`.

## Quick checks (read-only)
```bash
kubectl -n shop get pods
kubectl -n shop describe pod <pod> | grep -A8 -E 'Liveness|Readiness'
kubectl -n shop get events --field-selector reason=Unhealthy --sort-by=.lastTimestamp
kubectl -n shop get deploy <deployment> -o jsonpath='{.spec.template.spec.containers[0].livenessProbe}'
kubectl -n shop logs <pod> --previous --tail=30
kubectl -n shop rollout history deploy/<deployment>
```
Shopfront services serve liveness on `GET /healthz` on port `http` (8080), period 10s,
failure threshold 3. Redis uses a TCP check on 6379.

## Diagnosis decision tree
1. Is the termination reason `OOMKilled`? Then use the OOM runbook; the probe is not the cause.
2. What does the probe failure say?
   - `statuscode: 404`: the path is wrong. Compare the probe path with the endpoints
     the service actually serves (`/healthz`).
   - `connection refused`: wrong port, or the process is not listening yet. Check the
     named port and whether `STARTUP_DELAY_S` was raised without a startup probe.
   - `context deadline exceeded`: the endpoint is too slow for `timeoutSeconds`.
     Check CPU throttling and whether the liveness endpoint calls dependencies
     (it must not).
3. Did the probe change in the latest revision? A probe edit plus a restart loop that
   started at the same time is the strongest signal.

## Remediation options
- Correct the probe path, port or timeout (`patch_probe`), or roll back the revision
  that changed it (`rollback_deployment`).
- For slow starts, add a startup probe instead of a long `initialDelaySeconds`.

## Do NOT
- Do not delete the liveness probe as a fix; deadlocked processes would then never restart.
- Do not point liveness at an endpoint that checks dependencies; a redis blip would
  then restart every service.

## Verify recovery
- No new `Unhealthy` events for 5 minutes.
- Restart count stays constant; pod remains Ready.

## Escalation
Owning team. If probes fail across all services at once, escalate to team-platform
(node CPU starvation or kubelet issues).

## Related
- [rb-readiness-probe-failures](rb-readiness-probe-failures.md)
- [rb-oom-killed](rb-oom-killed.md)
- [pm-2026-05-orders-liveness-timeout](../postmortems/pm-2026-05-orders-liveness-timeout.md)
- [k8s-probes](../k8s-docs/k8s-probes.md)
