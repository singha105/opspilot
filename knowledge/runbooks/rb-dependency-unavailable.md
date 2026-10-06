---
id: rb-dependency-unavailable
title: Downstream dependency unreachable (redis or another service)
doc_type: runbook
categories: [DEPENDENCY_UNAVAILABLE]
services: [orders-api, inventory-api, redis, payments-api]
tags: [dependency, redis, connection-refused, cascade]
severity: high
last_reviewed: 2026-08-22
related: [rb-readiness-probe-failures, rb-service-misconfig, rb-dns-resolution-failures, svc-redis, pm-2026-02-redis-eviction-outage]
---
# Downstream dependency unreachable (redis or another service)

## Symptoms
- Several services go NotReady at the same time with zero restarts.
- Logs repeat `call to redis failed` (or `call to payments failed`) with errors such as
  `ConnectionRefusedError: [Errno 111] Connection refused`,
  `TimeoutError: timed out` or `Error 111 connecting to redis:6379`.
- `/readyz` returns 503 with a `dependencies` map showing which dependency is `false`.
- Alerts fire for the callers (`orders-api`, `inventory-api`) rather than for the
  dependency itself, which is easy to misread.

## Quick checks (read-only)
```bash
kubectl -n shop get deploy
kubectl -n shop get pods -l app.kubernetes.io/name=redis
kubectl -n shop get endpointslices -l kubernetes.io/service-name=redis
kubectl -n shop logs deploy/orders-api --tail=20
kubectl -n shop logs deploy/inventory-api --tail=20
kubectl -n shop get events --sort-by=.lastTimestamp | tail -20
kubectl -n shop rollout history deploy/redis
```
In Shopfront, `orders-api` depends on `payments-api` and `redis`; `inventory-api`
depends on `redis`. When both callers fail on the same dependency, look at that
dependency first.

## Diagnosis decision tree
1. Which dependency do the failing callers have in common? Use the `dependency`
   field in the JSON logs.
2. Does the dependency have running, Ready pods?
   - Deployment scaled to 0 replicas: someone scaled it down (maintenance, cost
     cleanup, a mistaken command). Check `spec.replicas`.
   - Pods crashing: follow the runbook for that pod's failure reason.
   - Pods Ready but Service has no endpoints: selector or port mismatch, see the
     service-misconfig runbook.
3. Is the error `Connection refused` (nothing listening, no endpoints) or a DNS error
   (`Name or service not known`)? DNS errors point to the DNS runbook.
4. Only one caller failing? Check that caller's own config (`REDIS_URL` value).

## Remediation options
- Scale the dependency back to its normal replica count (`scale_deployment`; redis
  runs 1 replica).
- If the dependency's latest rollout broke it, roll that Deployment back.
- Callers recover automatically once the dependency answers; no action is needed on them.

## Do NOT
- Do not restart the callers; they are reporting the problem correctly.
- Do not disable the callers' readiness probes.
- Do not flush or restart redis to "clear" it during an incident; Shopfront uses it
  as a cache and a cold cache increases load on payments.

## Verify recovery
- Dependency pods Ready and listed in its EndpointSlice.
- Callers log `call to redis returned 200` again and return to Ready within about a minute.

## Escalation
team-platform owns redis. Page them if redis is crashing or cannot be scaled back.

## Related
- [rb-readiness-probe-failures](rb-readiness-probe-failures.md)
- [rb-service-misconfig](rb-service-misconfig.md)
- [rb-dns-resolution-failures](rb-dns-resolution-failures.md)
- [svc-redis](../services/svc-redis.md)
- [pm-2026-02-redis-eviction-outage](../postmortems/pm-2026-02-redis-eviction-outage.md)
