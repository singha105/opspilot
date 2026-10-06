---
id: pm-2026-02-redis-eviction-outage
title: "Postmortem: redis evicted under node memory pressure, orders and inventory NotReady"
doc_type: postmortem
categories: [DEPENDENCY_UNAVAILABLE]
services: [redis, orders-api, inventory-api]
tags: [postmortem, redis, eviction, cascade, dependency]
date: 2026-02-09
severity: SEV-1
related: [rb-dependency-unavailable, rb-disk-pressure-evictions, svc-redis]
---
# Postmortem: redis evicted under node memory pressure, orders and inventory NotReady

## Summary
On 9 February 2026 the node running redis came under memory pressure while a batch job
ran. The kubelet evicted redis (a low-priority pod at the time). Its replacement stayed
Pending for 9 minutes until the batch job finished. With no redis endpoint, orders-api
and inventory-api logged `ConnectionRefusedError: [Errno 111] Connection refused` on
every call and reported NotReady, so checkout and product pages failed.

## Impact
- Checkout and stock lookups failed for 12 minutes (03:41–03:53 UTC, low traffic).

## Timeline (UTC)
- 03:30 Nightly analytics batch job started on the same node.
- 03:40 Node condition `MemoryPressure=True`.
- 03:41 redis pod `Evicted`: `The node was low on resource: memory`.
- 03:41 orders-api and inventory-api readiness fail; logs `call to redis failed`.
- 03:43 Pages for orders-api and inventory-api, but not for redis.
- 03:47 On-call restarted orders-api, which did not help.
- 03:50 On-call noticed both callers failed on the same dependency and checked redis.
- 03:53 Batch job finished, redis scheduled and Ready; callers recovered within 20 seconds.

## Root cause
The shared dependency (redis) was unavailable; the callers were healthy and reported the
problem correctly. Contributing: redis had no PriorityClass and a request well below its
real usage, so it was an early eviction candidate.

## Contributing factors
- Alerts fired on callers, not on the dependency.
- Restarting callers wasted time and briefly made them crash-loop.

## What went well
- Callers recovered automatically once redis returned.

## Action items
- Give redis a high PriorityClass and accurate requests. Owner: team-platform. Done.
- Alert on `kube_deployment_status_replicas_available{deployment="redis"} == 0`. Owner: team-platform.
- Runbook: find the common dependency before touching callers. Done.

## Related
- [rb-dependency-unavailable](../runbooks/rb-dependency-unavailable.md)
- [rb-disk-pressure-evictions](../runbooks/rb-disk-pressure-evictions.md)
- [svc-redis](../services/svc-redis.md)
