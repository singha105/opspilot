---
id: svc-orders-api
title: "Service card: orders-api"
doc_type: service_card
categories: []
services: [orders-api]
tags: [service-card, orders, ownership, slo]
owner_team: team-orders
last_reviewed: 2026-09-01
related: [svc-payments-api, svc-redis, rb-dependency-unavailable, rb-image-pull-error, pm-2025-11-orders-bad-image-tag, pm-2026-05-orders-liveness-timeout]
---
# Service card: orders-api

## Overview
`orders-api` creates and tracks customer orders. For each order it calls
`payments-api` to authorise payment and uses `redis` to cache cart and order state.
It is the service customers notice first: if either dependency fails, orders-api
reports NotReady and checkout stops.

## Ownership and escalation
- Owner: **team-orders** (Deployment annotation `owner-team: team-orders`).
- Primary on-call: PagerDuty schedule `orders-primary` (fictional), Slack `#orders-oncall`.
- Escalate payment failures to team-payments and redis failures to team-platform.

## Interfaces
| Item | Value |
|---|---|
| Namespace | `shop` |
| Deployment / Service | `orders-api` / `orders-api:8080` (port name `http`) |
| Liveness | `GET /healthz` every 10s, failure threshold 3 |
| Readiness | `GET /readyz` every 5s; ready only when payments and redis both answer |
| Dependencies | `payments=$(PAYMENTS_URL)`, `redis=$(REDIS_URL)` |

## Configuration
- `REQUIRED_ENV=REDIS_URL,PAYMENTS_URL`; a missing value logs
  `FATAL missing required env <NAME>` and exits with code 1.
- `REDIS_URL=redis://redis:6379` and `PAYMENTS_URL=http://payments-api:8080`, both from
  ConfigMap `shopfront-settings`.
- `LOG_LEVEL` from the same ConfigMap.

## Normal behaviour
- Memory 28–40 MiB; limit 128Mi, request 32Mi. CPU under 25m.
- Logs one JSON line per dependency call every 3–5 seconds, with `request_id`,
  `dependency`, `latency_ms` and `status`.

## SLOs
- 99.5% of order requests succeed over 30 days; p95 latency under 300 ms.

## Known failure modes
- NotReady because payments or redis is unavailable (most common page; usually not
  an orders-api bug).
- Image tag typos during release.
- Liveness timeouts under CPU throttling.

## Related
- [svc-payments-api](svc-payments-api.md)
- [svc-redis](svc-redis.md)
- [rb-dependency-unavailable](../runbooks/rb-dependency-unavailable.md)
- [rb-image-pull-error](../runbooks/rb-image-pull-error.md)
- [pm-2025-11-orders-bad-image-tag](../postmortems/pm-2025-11-orders-bad-image-tag.md)
- [pm-2026-05-orders-liveness-timeout](../postmortems/pm-2026-05-orders-liveness-timeout.md)
