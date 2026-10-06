---
id: rb-oom-killed
title: Container OOMKilled / exit code 137
doc_type: runbook
categories: [OOM_KILLED]
services: [payments-api, orders-api, inventory-api]
tags: [memory, crashloop, exit-137, limits]
severity: high
last_reviewed: 2026-08-14
related: [rb-insufficient-resources, rb-bad-rollout, svc-payments-api, pm-2026-03-payments-memory-leak, pm-2026-06-payments-ballast-config]
---
# Container OOMKilled / exit code 137

## Symptoms
- Alert `KubePodCrashLooping` for a Shopfront service in namespace `shop`.
- `kubectl get pods` shows `CrashLoopBackOff` or a climbing `RESTARTS` count.
- The container's last state is `Terminated` with reason `OOMKilled` and exit code 137.
- Callers log timeouts or `Connection refused` while the pod restarts; `orders-api`
  often goes NotReady when `payments-api` is the one being killed.

## Quick checks (read-only)
```bash
kubectl -n shop get pods -l app.kubernetes.io/part-of=shopfront
kubectl -n shop describe pod <pod>            # Last State, Reason, Exit Code, Limits
kubectl -n shop get pod <pod> -o jsonpath='{.status.containerStatuses[0].lastState.terminated}'
kubectl -n shop get events --sort-by=.lastTimestamp | tail -20
kubectl -n shop logs <pod> --previous --tail=50
kubectl -n shop get deploy <deployment> -o jsonpath='{.spec.template.spec.containers[0].resources}'
kubectl -n shop rollout history deploy/<deployment>
```
Shopfront app containers run with a 128Mi memory limit and normally use 25–40 MiB.
A pod that is killed within seconds of starting is allocating far more than that.

## Diagnosis decision tree
1. Is the termination reason `OOMKilled` (exit 137)? If the reason is `Error` with
   another exit code, this is not a memory kill: see the bad-command and missing-env runbooks.
2. Did memory change with a deploy or config change? Compare the current template
   with the previous revision (`rollout history --revision=N`). Look for new env vars
   such as `MEMORY_BALLAST_MB`, larger cache sizes, or a lowered `limits.memory`.
   If yes, the config change is the cause.
3. No config change, and the pod ran for hours before dying? Memory grows over time:
   suspect a leak in the application. Check the postmortem history for the service.
4. Is the node itself under memory pressure (`kubectl describe node`, condition
   `MemoryPressure`)? Then pods are evicted rather than OOMKilled: use the disk and
   memory pressure eviction runbook instead.

## Remediation options
- **Config regression:** roll back the Deployment to the last good revision
  (`rollback_deployment`), or remove the offending setting.
- **Limit too low for a real workload change:** raise `resources.limits.memory`
  (`patch_container_resources`) after confirming the new usage with the owning team.
  Keep requests realistic so the scheduler still places the pod.
- **Leak:** roll back to the previous image if the leak arrived with a release, then
  hand the investigation to the owning team.

## Do NOT
- Do not remove the memory limit entirely; one leaking pod can then starve the node.
- Do not delete pods in a loop; the Deployment recreates them with the same spec.
- Do not raise the limit above 512Mi on the single-node demo cluster.

## Verify recovery
- `kubectl -n shop rollout status deploy/<deployment>` completes.
- Restart count stays flat for 10 minutes and the pod reports Ready.
- Dependent services (`orders-api` for payments) return to Ready.

## Escalation
Page the owning team from the service card. If two services are OOMKilled at once,
escalate to team-platform: the node may be overcommitted.

## Related
- [rb-insufficient-resources](rb-insufficient-resources.md)
- [rb-bad-rollout](rb-bad-rollout.md)
- [svc-payments-api](../services/svc-payments-api.md)
- [pm-2026-03-payments-memory-leak](../postmortems/pm-2026-03-payments-memory-leak.md)
- [pm-2026-06-payments-ballast-config](../postmortems/pm-2026-06-payments-ballast-config.md)
- [k8s-assign-memory-resource](../k8s-docs/k8s-assign-memory-resource.md)
