---
id: pm-2026-01-inventory-missing-redis-url
title: "Postmortem: inventory-api crash loop after REDIS_URL was dropped in a refactor"
doc_type: postmortem
categories: [CONFIG_MISSING_ENV]
services: [inventory-api]
tags: [postmortem, env, config-refactor, crashloop]
date: 2026-01-14
severity: SEV-2
related: [rb-missing-env-var, svc-inventory-api]
---
# Postmortem: inventory-api crash loop after REDIS_URL was dropped in a refactor

## Summary
On 14 January 2026 a manifest refactor moved shared settings into the
`shopfront-settings` ConfigMap. The `REDIS_URL` entry was removed from inventory-api's
container env but never re-added as a `configMapKeyRef`. inventory-api requires the
variable, so every new container logged `FATAL missing required env REDIS_URL` and
exited with code 1. Product pages showed "stock unknown" for 26 minutes.

## Impact
- Stock levels unavailable on product pages for 26 minutes; checkout fell back to
  optimistic reservation, causing 41 oversold items that support refunded.

## Timeline (UTC)
- 10:02 Refactor PR merged and deployed to all three services.
- 10:03 inventory-api pod restarts; reason `Error`, exit code 1, uptime under a second.
- 10:06 `KubePodCrashLooping` for inventory-api.
- 10:10 On-call suspected redis because the service depends on it; redis was healthy.
- 10:19 `kubectl logs --previous` showed the FATAL line naming `REDIS_URL`.
- 10:24 Diff confirmed the env entry was gone; orders-api had been migrated correctly.
- 10:28 Rollback of inventory-api's Deployment.
- 10:29 Pod Ready.

## Root cause
A configuration change removed a required environment variable. The application failed
fast with a clear message; the delay came from investigating redis before reading the
previous container's logs.

## Contributing factors
- The refactor was tested on orders-api only.
- Crash-looping containers have no current logs; `--previous` is needed.

## What went well
- Fail-fast validation (`REQUIRED_ENV`) made the cause explicit in one log line.

## Action items
- CI renders all overlays and checks every `REQUIRED_ENV` name is defined. Owner: team-inventory.
- Runbook: read `logs --previous` before investigating dependencies. Done.

## Related
- [rb-missing-env-var](../runbooks/rb-missing-env-var.md)
- [svc-inventory-api](../services/svc-inventory-api.md)
