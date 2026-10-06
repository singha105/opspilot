---
id: rb-cronjob-failures
title: CronJob runs failing, missed or piling up
doc_type: runbook
categories: []
services: [inventory-api]
tags: [cronjob, jobs, batch, schedule]
severity: low
last_reviewed: 2026-05-20
related: [rb-bad-command, rb-missing-env-var]
---
# CronJob runs failing, missed or piling up

## Symptoms
- Alert `KubeJobFailed` or `CronJobNotRunRecently` for the nightly
  `inventory-reconcile` job.
- `kubectl get jobs` shows failed Jobs with `BackoffLimitExceeded`, or many active Jobs
  overlapping each other.
- CronJob status `lastScheduleTime` is old; events say
  `Cannot determine if job needs to be started: too many missed start times`.

## Quick checks (read-only)
```bash
kubectl -n shop get cronjobs
kubectl -n shop describe cronjob inventory-reconcile
kubectl -n shop get jobs --sort-by=.metadata.creationTimestamp
kubectl -n shop describe job <job>
kubectl -n shop logs job/<job> --tail=50
kubectl -n shop get events --field-selector involvedObject.kind=Job --sort-by=.lastTimestamp
```
`inventory-reconcile` runs at 02:00 UTC with `concurrencyPolicy: Forbid`,
`backoffLimit: 2` and `startingDeadlineSeconds: 600`.

## Diagnosis decision tree
1. Did the Job's pods run and fail, or did no Job get created?
2. Pods failed: read their logs. Exit codes and missing configuration are diagnosed the
   same way as for long-running services (see the bad-command and missing-env runbooks).
3. No Job created: is the CronJob suspended (`spec.suspend: true`)? Was the controller
   down longer than `startingDeadlineSeconds`? Is a previous run still active with
   `concurrencyPolicy: Forbid`?
4. Jobs piling up: runs take longer than the schedule interval; look at what the job
   waits on (redis, payments) and at its `activeDeadlineSeconds`.
5. Timezone surprise? Without `spec.timeZone`, schedules use the controller's time
   zone (UTC here); a job "missing" at 02:00 local time may have run hours earlier.

## Remediation options
- Fix the job's configuration through the owning team, then trigger a one-off run
  with `kubectl create job --from=cronjob/inventory-reconcile` (needs write access;
  on-call with the operator role cannot do this).
- Unsuspend a CronJob that was suspended by mistake.

## Do NOT
- Do not delete the CronJob; its history is needed to understand missed runs.
- Do not shorten the schedule to "catch up"; overlapping reconciles conflict.

## Verify recovery
- Next scheduled run completes; `lastSuccessfulTime` updates.

## Escalation
team-inventory owns the reconcile job.

## Related
- [rb-bad-command](rb-bad-command.md)
- [rb-missing-env-var](rb-missing-env-var.md)
