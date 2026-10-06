---
id: svc-payments-api
title: "Service card: payments-api"
doc_type: service_card
categories: []
services: [payments-api]
tags: [service-card, payments, ownership, slo]
owner_team: team-payments
last_reviewed: 2026-09-01
related: [rb-oom-killed, rb-readiness-probe-failures, pm-2026-03-payments-memory-leak, pm-2026-06-payments-ballast-config, pm-2026-04-payments-readiness-path]
---
# Service card: payments-api

## Overview
`payments-api` authorises and records payments for Shopfront checkouts. It is called
synchronously by `orders-api` for every order, so when payments is down, order
creation fails too. It has no downstream dependencies of its own in the demo
environment (the card processor is stubbed).

## Ownership and escalation
- Owner: **team-payments** (Deployment annotation `owner-team: team-payments`).
- Primary on-call: PagerDuty schedule `payments-primary` (fictional), Slack `#payments-oncall`.
- Secondary: team-platform for cluster, node or networking issues.

## Interfaces
| Item | Value |
|---|---|
| Namespace | `shop` |
| Deployment / Service | `payments-api` / `payments-api:8080` (port name `http`) |
| Liveness | `GET /healthz` every 10s, failure threshold 3 |
| Readiness | `GET /readyz` every 5s, failure threshold 2 |
| Work endpoint | `GET /work` (called by orders-api) |
| Labels | `app.kubernetes.io/name=payments-api`, `app.kubernetes.io/part-of=shopfront` |

## Configuration
- `SERVICE_NAME=payments-api`, `PORT=8080`.
- `LOG_LEVEL` from ConfigMap `shopfront-settings` (DEBUG, INFO, WARNING, ERROR; any
  other value makes the process exit with a FATAL log).
- No `REQUIRED_ENV` and no `DEPENDENCIES` in the demo build.
- `MEMORY_BALLAST_MB` must be unset in production; it exists only for load testing.

## Normal behaviour
- Memory: 25–35 MiB working set; limit 128Mi, request 32Mi.
- CPU: under 20m at demo load; limit 200m, request 10m.
- One replica in the demo; rollouts replace the pod in place (`maxSurge: 0`).

## SLOs
- Availability 99.9% of `/work` calls succeed over 30 days.
- Latency p95 under 150 ms measured at orders-api.

## Known failure modes
- OOMKilled after memory-related config changes or a leak (two postmortems).
- Readiness path mismatch after a framework upgrade removed `/readyz` aliases.

## Related
- [rb-oom-killed](../runbooks/rb-oom-killed.md)
- [rb-readiness-probe-failures](../runbooks/rb-readiness-probe-failures.md)
- [pm-2026-03-payments-memory-leak](../postmortems/pm-2026-03-payments-memory-leak.md)
- [pm-2026-06-payments-ballast-config](../postmortems/pm-2026-06-payments-ballast-config.md)
- [pm-2026-04-payments-readiness-path](../postmortems/pm-2026-04-payments-readiness-path.md)
