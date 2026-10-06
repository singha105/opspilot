---
id: pm-2026-06-payments-ballast-config
title: "Postmortem: payments-api crash loop after a load-test setting reached production"
doc_type: postmortem
categories: [OOM_KILLED]
services: [payments-api, orders-api]
tags: [postmortem, config-change, oomkilled, env]
date: 2026-06-17
severity: SEV-1
related: [rb-oom-killed, svc-payments-api, pm-2026-03-payments-memory-leak]
---
# Postmortem: payments-api crash loop after a load-test setting reached production

## Summary
On 17 June 2026 payments-api went into `CrashLoopBackOff` seconds after a
configuration-only rollout. A load-testing variable, `MEMORY_BALLAST_MB=300`, was
copied from the staging overlay into production. The service allocates the ballast at
startup, so every new container exceeded its 128Mi memory limit almost immediately and
was OOMKilled. Because Deployments roll with `maxSurge: 0`, the old healthy pod had
already been replaced, and checkout was fully down for 18 minutes.

## Impact
- 100% of checkouts failed for 18 minutes (14:02–14:20 UTC).
- orders-api reported NotReady for the same period because its payments dependency failed.

## Timeline (UTC)
- 14:01 Config-only change merged: "sync staging env into prod overlay".
- 14:02 New payments-api pod starts, exits with `OOMKilled`, exit code 137, after 3 seconds.
- 14:03 `KubePodCrashLooping` and `KubeDeploymentReplicasMismatch` page on-call.
- 14:07 On-call saw OOMKilled and initially suspected the March memory leak.
- 14:12 Uptime before each kill was 2–4 seconds, not 40 minutes, which ruled out a leak.
- 14:15 `rollout history` diff showed the new `MEMORY_BALLAST_MB` env var.
- 14:18 `kubectl rollout undo deploy/payments-api`.
- 14:20 Pod Ready; orders-api Ready a few seconds later.

## Root cause
A configuration change, not code: a test-only variable made the process allocate 300 MiB
at startup, above the 128Mi limit. Same symptom as the March incident (OOMKilled, exit
137), different cause and different fix: roll back the config rather than fix code.

## Contributing factors
- The staging and production overlays share a base, and the sync script copied all env vars.
- No policy rejected test-only variables in production.

## What went well
- The revision diff pointed directly at the change; rollback took under a minute.

## Action items
- Block `MEMORY_BALLAST_MB` and other test knobs in production with an admission policy.
  Owner: team-platform.
- Sync script copies an explicit allowlist of variables. Owner: team-payments.
- Runbook update: compare uptime-before-kill to tell leaks from config changes. Done.

## Related
- [rb-oom-killed](../runbooks/rb-oom-killed.md)
- [svc-payments-api](../services/svc-payments-api.md)
- [pm-2026-03-payments-memory-leak](pm-2026-03-payments-memory-leak.md)
