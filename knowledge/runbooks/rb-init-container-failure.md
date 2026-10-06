---
id: rb-init-container-failure
title: Pod stuck in Init because an init container fails or never finishes
doc_type: runbook
categories: [INIT_CONTAINER_FAILURE]
services: [payments-api, orders-api, inventory-api]
tags: [init-containers, startup, wait-for, migrations]
severity: medium
last_reviewed: 2026-07-25
related: [rb-dependency-unavailable, rb-bad-command, k8s-init-containers]
---
# Pod stuck in Init because an init container fails or never finishes

## Symptoms
- `kubectl get pods` shows `Init:0/1`, `Init:Error` or `Init:CrashLoopBackOff`.
- The main container never starts, so it has no logs; the Deployment shows 0 available.
- Common Shopfront pattern: an init container waits for redis (`wait-for redis:6379`)
  or runs a schema migration before `payments-api` starts.

## Quick checks (read-only)
```bash
kubectl -n shop get pods
kubectl -n shop describe pod <pod> | sed -n '/Init Containers/,/Containers:/p'
kubectl -n shop logs <pod> -c <init-container> --previous
kubectl -n shop logs <pod> -c <init-container>
kubectl -n shop get pod <pod> -o jsonpath='{.status.initContainerStatuses}'
kubectl -n shop get events --sort-by=.lastTimestamp | tail -20
```

## Diagnosis decision tree
1. Is the init container **failing** (`Init:Error`, `Init:CrashLoopBackOff`) or
   **waiting forever** (`Init:0/1`, running, no exit)?
2. Failing: read its logs. A non-zero exit from a migration means the migration itself
   is broken; exit 127 means its command is wrong (see the bad-command runbook).
3. Waiting forever: what is it waiting for? A wait-for loop that never succeeds means
   the dependency is down or the address is wrong. Check the dependency the same way
   as in the dependency runbook.
4. Did the init container spec change in the latest revision (new image, new command,
   new timeout)?
5. Is the init container OOMKilled? Init containers have their own resource limits; a
   migration that loads a large table can exceed a limit sized for a tiny wait loop.

## Remediation options
- Fix or restore the dependency the init container waits for.
- Roll back a revision that introduced a broken init container (`rollback_deployment`).

## Do NOT
- Do not remove a migration init container to force the app to start; the app may then
  run against an old schema.

## Verify recovery
- Init containers show `Completed`, the main container becomes Running and Ready.

## Escalation
Owning team; team-platform if the waited-for dependency is shared infrastructure.

## Related
- [rb-dependency-unavailable](rb-dependency-unavailable.md)
- [rb-bad-command](rb-bad-command.md)
- [k8s-init-containers](../k8s-docs/k8s-init-containers.md)
