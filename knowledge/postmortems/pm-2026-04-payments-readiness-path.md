---
id: pm-2026-04-payments-readiness-path
title: "Postmortem: payments-api never became Ready after a probe path change"
doc_type: postmortem
categories: [READINESS_PROBE_MISCONFIG]
services: [payments-api, orders-api]
tags: [postmortem, readiness, probes, endpoints]
date: 2026-04-22
severity: SEV-1
related: [rb-readiness-probe-failures, svc-payments-api, rb-dependency-unavailable]
---
# Postmortem: payments-api never became Ready after a probe path change

## Summary
On 22 April 2026 a manifest change renamed the payments-api readiness probe path to
`/ready` to match a new internal standard. The service only serves `/readyz`, so the
probe got `404` on every check. The pod ran normally but was never Ready, the Service
had no endpoints, and orders-api failed every call to payments with
`Connection refused` and went NotReady itself. Checkout was down for 22 minutes.

## Impact
- 100% of checkouts failed for 22 minutes (16:05–16:27 UTC).

## Timeline (UTC)
- 16:04 Probe standardisation PR deployed to payments-api.
- 16:05 New pod `Running`, `0/1` Ready, zero restarts. Events:
  `Readiness probe failed: HTTP probe failed with statuscode: 404`.
- 16:06 orders-api logs `call to payments failed ... Connection refused`; orders-api NotReady.
- 16:08 Page for orders-api (`KubeDeploymentReplicasMismatch`).
- 16:15 On-call investigated orders-api, then payments-api because orders blamed it.
- 16:21 Noticed payments-api had no restarts and no errors in its own logs, only a 404 probe.
- 16:25 Rollback of payments-api.
- 16:27 Endpoints populated; orders-api Ready.

## Root cause
A readiness probe misconfiguration: the probe pointed at a path the application does not
serve. The application itself was healthy. The difference between `404` (wrong path)
and `503` (service reports a real dependency problem) was the decisive clue.

## Contributing factors
- The probe change was not tested against the running service.
- The cascade made the first page point at orders-api.

## What went well
- Rollback was immediate once the 404 was spotted.

## Action items
- CI smoke test hits every probe path on a running container. Owner: team-payments.
- Dashboards show probe failure reasons per Deployment. Owner: team-platform.

## Related
- [rb-readiness-probe-failures](../runbooks/rb-readiness-probe-failures.md)
- [rb-dependency-unavailable](../runbooks/rb-dependency-unavailable.md)
- [svc-payments-api](../services/svc-payments-api.md)
