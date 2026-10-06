---
id: svc-redis
title: "Service card: redis (Shopfront cache)"
doc_type: service_card
categories: []
services: [redis]
tags: [service-card, redis, cache, ownership]
owner_team: team-platform
last_reviewed: 2026-09-01
related: [svc-orders-api, svc-inventory-api, rb-dependency-unavailable, pm-2026-02-redis-eviction-outage]
---
# Service card: redis (Shopfront cache)

## Overview
A single redis instance shared by `orders-api` (cart and order cache) and
`inventory-api` (stock counters). It runs without persistence: data is a cache and is
rebuilt after a restart. Losing redis does not lose orders, but both callers report
NotReady until it is back, which stops checkout.

## Ownership and escalation
- Owner: **team-platform** (Deployment annotation `owner-team: team-platform`).
- On-call: PagerDuty schedule `platform-primary` (fictional), Slack `#platform-oncall`.

## Interfaces
| Item | Value |
|---|---|
| Namespace | `shop` |
| Deployment / Service | `redis` / `redis:6379` (port name `redis`) |
| Image | `redis:7-alpine` |
| Liveness | TCP check on 6379 every 10s |
| Readiness | `redis-cli ping` every 5s |
| Consumers | `orders-api`, `inventory-api` (via `REDIS_URL=redis://redis:6379`) |

## Configuration
- Arguments: `--save "" --appendonly no --maxmemory 32mb`.
- Runs as uid 999 with a read-only root filesystem and an `emptyDir` at `/data`.
- Replicas: exactly 1. Scaling to 0 is an outage for both consumers.

## Normal behaviour
- Memory 6–12 MiB; limit 64Mi, request 32Mi. CPU under 10m.
- Callers see PING latency of 1–5 ms.

## SLOs
- 99.95% availability; it is a tier-0 dependency for checkout.

## Known failure modes
- Evicted during node memory pressure, leaving both callers NotReady until it was
  rescheduled (postmortem 2026-02).
- Scaled to zero by hand or by cleanup tooling that matches it by label.
- Connection refused for callers when the Service selector or port name drifts.

## Related
- [svc-orders-api](svc-orders-api.md)
- [svc-inventory-api](svc-inventory-api.md)
- [rb-dependency-unavailable](../runbooks/rb-dependency-unavailable.md)
- [pm-2026-02-redis-eviction-outage](../postmortems/pm-2026-02-redis-eviction-outage.md)
