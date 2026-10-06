---
id: pm-2026-03-payments-memory-leak
title: "Postmortem: payments-api restarts from a slow memory leak in the retry cache"
doc_type: postmortem
categories: [OOM_KILLED]
services: [payments-api, orders-api]
tags: [postmortem, memory-leak, oomkilled, release]
date: 2026-03-11
severity: SEV-2
related: [rb-oom-killed, rb-bad-rollout, svc-payments-api, pm-2026-06-payments-ballast-config]
---
# Postmortem: payments-api restarts from a slow memory leak in the retry cache

## Summary
For about six hours on 11 March 2026, payments-api restarted roughly every 40 minutes.
Each restart made orders-api NotReady for 20–30 seconds and failed the checkouts in
flight. The cause was a memory leak introduced in release 4.18: a retry cache kept
every failed card-processor response forever. Memory grew steadily until the container
hit its 128Mi limit and was OOMKilled.

## Impact
- 1.6% of checkouts failed between 09:10 and 15:20 UTC (about 2,300 orders).
- No double charges; the payment ledger stayed consistent.

## Timeline (UTC)
- 08:55 Release 4.18 of payments-api rolled out. No alerts.
- 09:38 First restart. Last state `OOMKilled`, exit code 137, after 43 minutes of uptime.
- 10:20 `KubePodCrashLooping` did not fire: restarts were too far apart for the alert.
- 11:05 Support reported intermittent checkout failures. On-call looked at orders-api first.
- 12:40 On-call noticed the restart count on payments-api and the regular 40-minute rhythm.
- 13:10 Memory graph showed a saw-tooth: linear growth from 30 MiB to 128 MiB, then a drop.
- 14:50 team-payments found the unbounded retry cache.
- 15:20 Rolled back to 4.17. Memory flat at 31 MiB since.

## Root cause
A code change in 4.18 added a dictionary of failed responses for retries but never
evicted entries. Usage grew with traffic, not with configuration: no env var, request
or limit changed in the rollout. The key signal was a long uptime before each OOM kill
and a linear memory slope.

## Contributing factors
- Crash-loop alerting needs several restarts in 15 minutes, so slow loops went unnoticed.
- There was no alert on memory working set approaching the limit.

## What went well
- Rollback restored service within minutes once the cause was known.

## Action items
- Bound the retry cache (LRU, 1,000 entries). Owner: team-payments. Done.
- Alert when working set exceeds 85% of the memory limit for 10 minutes. Owner: team-platform.
- Add a restart-rate alert over a 2-hour window. Owner: team-platform.

## Related
- [rb-oom-killed](../runbooks/rb-oom-killed.md)
- [rb-bad-rollout](../runbooks/rb-bad-rollout.md)
- [svc-payments-api](../services/svc-payments-api.md)
- [pm-2026-06-payments-ballast-config](pm-2026-06-payments-ballast-config.md)
