---
id: svc-inventory-api
title: "Service card: inventory-api"
doc_type: service_card
categories: []
services: [inventory-api]
tags: [service-card, inventory, ownership, slo]
owner_team: team-inventory
last_reviewed: 2026-09-01
related: [svc-redis, rb-missing-env-var, rb-dependency-unavailable, pm-2026-01-inventory-missing-redis-url, pm-2026-08-inventory-service-selector]
---
# Service card: inventory-api

## Overview
`inventory-api` answers stock-level queries for product pages and reserves stock
during checkout. Stock counters live in `redis`. A nightly CronJob,
`inventory-reconcile`, corrects counters against the warehouse feed.

## Ownership and escalation
- Owner: **team-inventory** (Deployment annotation `owner-team: team-inventory`).
- Primary on-call: PagerDuty schedule `inventory-primary` (fictional), Slack `#inventory-oncall`.
- Redis problems go to team-platform.

## Interfaces
| Item | Value |
|---|---|
| Namespace | `shop` |
| Deployment / Service | `inventory-api` / `inventory-api:8080` (port name `http`) |
| Liveness | `GET /healthz` every 10s |
| Readiness | `GET /readyz` every 5s; ready only when redis answers PING |
| Dependencies | `redis=$(REDIS_URL)` |

## Configuration
- `REQUIRED_ENV=REDIS_URL`. Without it the process logs
  `FATAL missing required env REDIS_URL` and exits 1 immediately.
- `REDIS_URL=redis://redis:6379` from ConfigMap `shopfront-settings`.
- `LOG_LEVEL` from the same ConfigMap.

## Normal behaviour
- Memory 22–30 MiB; limit 128Mi, request 32Mi. CPU under 15m.
- Redis PING latency in logs normally 1–5 ms.

## SLOs
- 99.9% of stock queries succeed over 30 days; p95 latency under 100 ms.

## Known failure modes
- Crash loop when `REDIS_URL` is dropped from the Deployment during config refactors.
- NotReady when redis is scaled down or evicted.
- Service selector drift after a label rename (postmortem 2026-08).

## Related
- [svc-redis](svc-redis.md)
- [rb-missing-env-var](../runbooks/rb-missing-env-var.md)
- [rb-dependency-unavailable](../runbooks/rb-dependency-unavailable.md)
- [pm-2026-01-inventory-missing-redis-url](../postmortems/pm-2026-01-inventory-missing-redis-url.md)
- [pm-2026-08-inventory-service-selector](../postmortems/pm-2026-08-inventory-service-selector.md)
